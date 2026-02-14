"""Entry point principal — FastAPI + background polling loop."""
import asyncio
import sys
from datetime import datetime
from contextlib import asynccontextmanager

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

print("=== POLYMARKET ALERT BOT v2.0 INICIANDO ===", flush=True)

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src import config
from src.api.polymarket import PolymarketClient
from src.storage.database import Database
from src.detection.analyzer import AnomalyAnalyzer
from src.alerts.telegram import TelegramNotifier
from src.api.routes import router

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger()


# ── Bot Class ────────────────────────────────────────────────────────

class PolymarketAlertBot:
    """Orquesta polling, detección, alertas y resolución."""

    def __init__(self):
        self.db = Database()
        self.analyzer = AnomalyAnalyzer()
        self.notifier = TelegramNotifier()
        self.start_time = datetime.now()
        self.trades_processed = 0
        self.alerts_sent = 0
        self._running = False

    async def start(self):
        """Inicializar DB y marcar como running."""
        try:
            await self.db.connect()
            print("DB PostgreSQL conectada OK", flush=True)
        except Exception as e:
            print(f"ERROR conectando DB: {e}", flush=True)
            raise
        self._running = True
        # Telegram startup en background para no bloquear healthcheck
        asyncio.create_task(self._send_startup_safe())

    async def _send_startup_safe(self):
        try:
            await self.notifier.send_startup_message()
            print("Mensaje startup enviado", flush=True)
        except Exception as e:
            print(f"Error enviando startup a Telegram: {e}", flush=True)

    async def stop(self):
        self._running = False
        await self.db.close()

    # ── Polling principal ─────────────────────────────────────────────

    async def run_polling_loop(self):
        """Loop infinito de polling."""
        print(f"Iniciando polling loop (cada {config.POLL_INTERVAL}s)", flush=True)
        cycle = 0
        while self._running:
            try:
                await self.poll_cycle()
                cycle += 1
                # Cada 10 ciclos, actualizar baselines y chequear resoluciones
                if cycle % 10 == 0:
                    await self.update_all_baselines()
                # Cada 30 ciclos (~30 min), chequear resoluciones
                if cycle % 30 == 0:
                    await self.check_resolutions()
                # Cada 60 ciclos (~1h), enviar health check
                if cycle % 60 == 0:
                    await self.send_health_check()
            except Exception as e:
                print(f"Error en ciclo polling: {e}", flush=True)
                import traceback
                traceback.print_exc()
            await asyncio.sleep(config.POLL_INTERVAL)

    async def poll_cycle(self):
        print("--- Ciclo de polling ---", flush=True)
        async with PolymarketClient() as client:
            # Pre-cargar cache de mercados para enriquecer trades
            await client.get_markets(limit=config.MAX_MARKETS)

            trades = await client.get_recent_trades(limit=500)

            if not trades:
                print("Trades obtenidos: 0", flush=True)
                return

            sizes = [t.size for t in trades]
            max_s = max(sizes)
            avg_s = sum(sizes) / len(sizes)
            big = sum(1 for s in sizes if s >= config.MIN_SIZE_USD)
            print(
                f"Trades: {len(trades)} | max=${max_s:,.0f} avg=${avg_s:,.0f} | "
                f"{big} >= ${config.MIN_SIZE_USD} (pre-filtro)",
                flush=True,
            )

            for trade in trades:
                await self.process_trade(trade, client)

        logger.info("ciclo_completo", procesados=self.trades_processed, alertas=self.alerts_sent)

    async def process_trade(self, trade, client: PolymarketClient):
        # Skip duplicados
        if await self.db.is_trade_processed(trade.transaction_hash):
            return

        # Registrar trade y wallet siempre
        await self.db.record_trade(trade)
        await self.db.update_wallet_stats(trade)
        self.trades_processed += 1

        # Trackear mercado para resolución futura
        if trade.market_id:
            await self.db.track_market(
                trade.market_id, trade.market_question, trade.market_slug,
                trade.market_end_date, trade.market_category,
            )

        # Skip trades pequeños para análisis
        if trade.size < config.MIN_SIZE_USD:
            return

        # Obtener contexto
        wallet_stats = await self.db.get_wallet_stats(trade.wallet_address)
        market_baseline = await self.db.get_market_baseline(trade.market_id)

        # Clustering: buscar wallets que apostaron igual recientemente
        cluster = await self.db.find_cluster_wallets(
            trade.market_id, trade.side, trade.outcome,
            window_minutes=30, min_size=1000,
        )

        # Enriquecer trade con datos del mercado si faltan
        if not trade.market_end_date and trade.market_id:
            market_data = await client.get_market_by_id(trade.market_id)
            if market_data and market_data.get("end_date"):
                trade.market_end_date = market_data["end_date"]

        # Analizar
        candidate = self.analyzer.analyze(trade, wallet_stats, market_baseline, cluster)

        if not self.analyzer.should_alert(candidate):
            return

        # Cooldown
        if not await self.db.should_alert(trade.wallet_address, trade.market_id, config.COOLDOWN_HOURS):
            return

        # Enviar alerta
        success = await self.notifier.send_alert(candidate)
        if success:
            self.alerts_sent += 1
            await self.db.record_alert(
                wallet=trade.wallet_address,
                market_id=trade.market_id,
                market_question=trade.market_question,
                market_slug=trade.market_slug,
                side=trade.side,
                outcome=trade.outcome,
                size=trade.size,
                price=trade.price,
                score=candidate.score,
                triggers=candidate.triggers,
                cluster_wallets=candidate.cluster_wallets,
                days_to_close=candidate.days_to_resolution,
                wallet_hit_rate=candidate.wallet_hit_rate,
            )
            logger.info("alerta_enviada",
                        wallet=trade.wallet_address[:10],
                        market=trade.market_slug,
                        score=candidate.score)

    # ── Baselines ─────────────────────────────────────────────────────

    async def update_all_baselines(self):
        """Actualizar baselines de todos los mercados con trades recientes."""
        try:
            async with self.db._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT DISTINCT market_id FROM trades
                    WHERE timestamp > NOW() - INTERVAL '24 hours'
                """)
            for r in rows:
                await self.db.update_market_baseline(r["market_id"])
            print(f"Baselines actualizados: {len(rows)} mercados", flush=True)
        except Exception as e:
            print(f"Error actualizando baselines: {e}", flush=True)

    # ── Resolution Checker ────────────────────────────────────────────

    async def check_resolutions(self):
        """Verificar si mercados trackeados se resolvieron."""
        try:
            unresolved = await self.db.get_unresolved_markets()
            if not unresolved:
                return

            print(f"Verificando resolución de {len(unresolved)} mercados...", flush=True)
            resolved_count = 0

            async with PolymarketClient() as client:
                for market in unresolved:
                    resolution = await client.check_market_resolution(market["condition_id"])
                    if resolution:
                        await self.db.resolve_market(market["condition_id"], resolution)
                        resolved_count += 1

                        # Contar alertas correctas para este mercado
                        alerts = await self.db.get_market_alerts(market["condition_id"])
                        correct = sum(1 for a in alerts if a.get("was_correct"))
                        total = len(alerts)

                        if total > 0:
                            await self.notifier.send_resolution_update(
                                market.get("question", ""),
                                resolution, correct, total,
                            )

            if resolved_count > 0:
                print(f"Mercados resueltos: {resolved_count}", flush=True)
        except Exception as e:
            print(f"Error verificando resoluciones: {e}", flush=True)

    # ── Health Check ──────────────────────────────────────────────────

    async def send_health_check(self):
        try:
            stats = await self.db.get_dashboard_stats()
            uptime = datetime.now() - self.start_time
            hours = int(uptime.total_seconds() // 3600)
            minutes = int((uptime.total_seconds() % 3600) // 60)
            stats["uptime"] = f"{hours}h {minutes}m"
            await self.notifier.send_health_check(stats)
        except Exception as e:
            print(f"Error en health check: {e}", flush=True)


# ── FastAPI App ──────────────────────────────────────────────────────

bot: PolymarketAlertBot | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle: iniciar bot + polling en background, cerrar al apagar."""
    global bot
    bot = PolymarketAlertBot()
    await bot.start()
    app.state.db = bot.db
    app.state.bot = bot
    # Lanzar polling en background
    polling_task = asyncio.create_task(bot.run_polling_loop())
    print(f"Dashboard activo en puerto {config.DASHBOARD_PORT}", flush=True)
    yield
    # Shutdown
    bot._running = False
    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass
    await bot.stop()


app = FastAPI(title="Polymarket Insider Alert Bot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


if __name__ == "__main__":
    print(f"=== INICIANDO SERVER EN PUERTO {config.DASHBOARD_PORT} ===", flush=True)
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=config.DASHBOARD_PORT,
        log_level="info",
    )
