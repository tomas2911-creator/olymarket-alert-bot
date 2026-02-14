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
from src.api.polygonscan import get_wallet_onchain_info

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
        self._watchlist: set[str] = set()

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

                # Cada 5 ciclos (~5 min): price impact check
                if cycle % 5 == 0:
                    await self.check_price_impact()

                # Cada 10 ciclos (~10 min): baselines + smart money scores
                if cycle % 10 == 0:
                    await self.update_all_baselines()
                    sm_count = await self.db.update_all_smart_money_scores()
                    if sm_count:
                        print(f"Smart money scores actualizados: {sm_count} wallets", flush=True)

                # Cada 30 ciclos (~30 min): resoluciones + watchlist
                if cycle % 30 == 0:
                    await self.check_resolutions()
                    await self.db.update_watchlist(
                        threshold=config.SMART_MONEY_THRESHOLD,
                        min_resolved=config.COPY_TRADE_MIN_RESOLVED,
                    )
                    self._watchlist = await self.db.get_watchlisted_wallets()
                    if self._watchlist:
                        print(f"Watchlist actualizado: {len(self._watchlist)} wallets", flush=True)

                # Cada 15 ciclos (~15 min): on-chain check (20 wallets por batch)
                if cycle % 15 == 0:
                    await self.check_onchain_wallets()

                # Cada 60 ciclos (~1h): health check + coordination
                if cycle % 60 == 0:
                    await self.send_health_check()
                    try:
                        coord_count = await self.db.detect_coordination()
                        if coord_count:
                            print(f"Coordinacion detectada: {coord_count} pares", flush=True)
                    except Exception as e:
                        print(f"Error en coordinacion: {e}", flush=True)

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

        # Skip trades pequeños para análisis (excepto wallets en watchlist)
        is_watched = trade.wallet_address.lower() in self._watchlist
        if trade.size < config.MIN_SIZE_USD and not is_watched:
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

        # v4.0: Contexto adicional para nuevas señales
        accumulation_info = None
        market_price = None
        smart_cluster_count = 0
        try:
            accumulation_info = await self.db.get_accumulation_info(
                trade.wallet_address, trade.market_id, trade.outcome)
            market_price = await client.get_market_price(trade.market_id, "Yes")
            smart_cluster_count = await self.db.count_smart_wallets_same_side(
                trade.market_id, trade.side, trade.outcome, trade.wallet_address)
        except Exception:
            pass  # No bloquear análisis si falla algún contexto extra

        # Analizar con todas las señales (v4.0)
        candidate = self.analyzer.analyze(
            trade, wallet_stats, market_baseline, cluster,
            accumulation_info=accumulation_info,
            market_price=market_price,
            smart_cluster_count=smart_cluster_count,
        )

        # Copy-trade: si wallet está en watchlist, alertar aunque score sea bajo
        is_copy = trade.wallet_address.lower() in self._watchlist
        should_alert = self.analyzer.should_alert(candidate) or is_copy

        if not should_alert:
            return

        # Cooldown
        if not await self.db.should_alert(trade.wallet_address, trade.market_id, config.COOLDOWN_HOURS):
            return

        # Enviar alerta (copy-trade o anomalía)
        if is_copy and not self.analyzer.should_alert(candidate):
            candidate.triggers.insert(0, "⭐ Smart Money (watchlisted)")
            success = await self.notifier.send_copy_trade_alert(trade, candidate)
        else:
            if is_copy:
                candidate.triggers.insert(0, "⭐ Smart Money")
            success = await self.notifier.send_alert(candidate)

        if success:
            self.alerts_sent += 1
            await self.db.record_alert_with_price(
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
                price_at_alert=market_price or trade.price,
                is_copy_trade=is_copy,
            )
            logger.info("alerta_enviada",
                        wallet=trade.wallet_address[:10],
                        market=trade.market_slug,
                        score=candidate.score,
                        copy_trade=is_copy)

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

    # ── Price Impact Checker ──────────────────────────────────────────

    async def check_price_impact(self):
        """Verificar cómo se movió el precio después de cada alerta."""
        try:
            checks = [
                ("price_1h", 1, 48),
                ("price_6h", 6, 48),
                ("price_24h", 24, 72),
            ]
            updated = 0
            async with PolymarketClient() as client:
                for field, min_h, max_h in checks:
                    alerts = await self.db.get_alerts_needing_price_check(field, min_h, max_h)
                    for alert in alerts:
                        price = await client.get_market_price(
                            alert["market_id"], alert.get("outcome", "Yes")
                        )
                        if price is not None:
                            await self.db.update_alert_price_impact(alert["id"], field, price)
                            updated += 1
            if updated:
                print(f"Price impact: {updated} alertas actualizadas", flush=True)
        except Exception as e:
            print(f"Error en price impact check: {e}", flush=True)

    # ── On-chain Checker (Polygonscan) ─────────────────────────────

    async def check_onchain_wallets(self):
        """Verificar datos on-chain de wallets nuevas (rate-limited)."""
        if not config.POLYGONSCAN_API_KEY:
            return
        try:
            wallets = await self.db.get_wallets_for_onchain_check(limit=20)
            for addr in wallets:
                info = await get_wallet_onchain_info(addr)
                # Guardar si hay al menos algún dato útil
                has_data = any(v is not None for v in info.values())
                if has_data:
                    await self.db.update_wallet_onchain(addr, info)
                await asyncio.sleep(2)  # Rate limit entre wallets
            if wallets:
                print(f"On-chain check: {len(wallets)} wallets", flush=True)
        except Exception as e:
            print(f"Error en on-chain check: {e}", flush=True)

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
