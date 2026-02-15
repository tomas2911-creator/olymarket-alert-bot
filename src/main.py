"""Entry point principal — FastAPI + background polling loop."""
import asyncio
import sys
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

print("=== POLYMARKET ALERT BOT v3.0 INICIANDO ===", flush=True)

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import httpx

from src import config
from src.api.polymarket import PolymarketClient
from src.storage.database import Database
from src.detection.analyzer import AnomalyAnalyzer
from src.alerts.telegram import TelegramNotifier
from src.api.routes import router
from src.api.polygonscan import get_wallet_onchain_info
from src.crypto_arb.binance_feed import BinanceFeed
from src.crypto_arb.detector import CryptoArbDetector
from src.crypto_arb.backtester import CryptoArbBacktester

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
        # Debug counters
        self._debug = {"too_small": 0, "scored": 0, "low_score": 0, "excluded_cat": 0, "cooldown": 0, "alerted": 0, "last_scores": []}
        self._watchlist: set[str] = set()
        self._excluded_categories: set[str] = set()
        # Crypto Arb (inicializado solo si feature está habilitada)
        self.binance_feed = None
        self.crypto_detector = None
        self.backtester = CryptoArbBacktester()

    async def start(self):
        """Inicializar DB y marcar como running."""
        try:
            await self.db.connect()
            print("DB PostgreSQL conectada OK", flush=True)
        except Exception as e:
            print(f"ERROR conectando DB: {e}", flush=True)
            raise
        self._running = True
        # Restaurar TODAS las configs desde DB (persisten entre reinicios)
        try:
            saved_cfg = await self.db.get_config()
            config.restore_from_db(saved_cfg)
            exc_str = saved_cfg.get("excluded_categories", "")
            self._excluded_categories = {c.strip().lower() for c in exc_str.split(",") if c.strip()}
            restored = len([k for k in saved_cfg if k != "excluded_categories"])
            if restored:
                print(f"Config restaurada desde DB: {restored} valores", flush=True)
        except Exception as e:
            print(f"Error restaurando config: {e}", flush=True)
            self._excluded_categories = set()
        # Telegram startup en background para no bloquear healthcheck
        asyncio.create_task(self._send_startup_safe())
        # Iniciar crypto arb si está habilitado
        if config.FEATURE_CRYPTO_ARB:
            await self._start_crypto_arb()

    async def _send_startup_safe(self):
        try:
            await self.notifier.send_startup_message()
            print("Mensaje startup enviado", flush=True)
        except Exception as e:
            print(f"Error enviando startup a Telegram: {e}", flush=True)

    async def _start_crypto_arb(self):
        """Inicializar módulo crypto arb."""
        try:
            pairs = [c["binance_pair"] for c in config.CRYPTO_ARB_COINS]
            self.binance_feed = BinanceFeed(pairs=pairs)
            self.crypto_detector = CryptoArbDetector(self.binance_feed)
            # Lanzar feed y detector como tasks
            asyncio.create_task(self._run_binance_feed())
            asyncio.create_task(self._run_crypto_detector())
            print(f"Crypto Arb iniciado: {len(pairs)} pares, modo={config.CRYPTO_ARB_MODE}", flush=True)
        except Exception as e:
            print(f"Error iniciando Crypto Arb: {e}", flush=True)

    async def _run_binance_feed(self):
        """Wrapper para Binance feed con reconexión."""
        while self._running and self.binance_feed:
            try:
                await self.binance_feed.start()
            except Exception as e:
                print(f"Binance feed error, reintentando en 10s: {e}", flush=True)
                await asyncio.sleep(10)

    async def _run_crypto_detector(self):
        """Wrapper para detector crypto con manejo de señales."""
        # Esperar 5 segundos para que el feed se conecte
        await asyncio.sleep(5)
        try:
            await self.crypto_detector.start()
        except Exception as e:
            print(f"Crypto detector error: {e}", flush=True)

    async def stop(self):
        self._running = False
        if self.binance_feed:
            await self.binance_feed.stop()
        if self.crypto_detector:
            await self.crypto_detector.stop()
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

            # Crypto arb: procesar y resolver en bloque separado
            # (no depende de poll_cycle — si el polling falla, crypto sigue)
            if config.FEATURE_CRYPTO_ARB:
                try:
                    await self.process_crypto_signals()
                    await self.resolve_crypto_signals()
                except Exception as e:
                    print(f"Error en crypto arb: {e}", flush=True)

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
            self._debug["too_small"] += 1
            return

        # Obtener contexto — envuelto en try/except para no abortar el ciclo
        try:
            wallet_stats = await self.db.get_wallet_stats(trade.wallet_address)
            market_baseline = await self.db.get_market_baseline(trade.market_id)
        except Exception as e:
            err_msg = f"contexto DB: {e} | wallet={trade.wallet_address[:12]} market={trade.market_slug}"
            print(f"ERROR {err_msg}", flush=True)
            self._debug["errors"] = self._debug.get("errors", 0) + 1
            self._debug["last_error"] = err_msg
            return

        # Clustering: buscar wallets que apostaron igual recientemente
        cluster = []
        try:
            cluster = await self.db.find_cluster_wallets(
                trade.market_id, trade.side, trade.outcome,
                window_minutes=30, min_size=1000,
            )
        except Exception:
            pass

        # Enriquecer trade con datos del mercado si faltan
        if not trade.market_end_date and trade.market_id:
            try:
                market_data = await client.get_market_by_id(trade.market_id)
                if market_data and market_data.get("end_date"):
                    trade.market_end_date = market_data["end_date"]
            except Exception:
                pass

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
            pass

        # v6.0: Wallet Baskets — detectar si wallet opera fuera de categoría
        wallet_category_shift = False
        cross_basket_count = 0
        if config.FEATURE_WALLET_BASKETS and trade.market_category:
            try:
                profile = await self.db.get_wallet_category_profile(trade.wallet_address)
                if profile["total_trades"] >= config.BASKET_MIN_WALLET_TRADES:
                    cat_info = profile["categories"].get(trade.market_category)
                    cat_pct = (cat_info["pct"] / 100) if cat_info else 0
                    if cat_pct < config.BASKET_CATEGORY_SHIFT_THRESHOLD:
                        wallet_category_shift = True
                    # Cuántas wallets fuera de categoría operan en este mercado
                    cross_wallets = await self.db.find_basket_wallets_in_market(
                        trade.market_id, trade.market_category)
                    cross_basket_count = len(cross_wallets)
            except Exception:
                pass

        # v6.0: Sniper DBSCAN — detectar cluster temporal estrecho
        sniper_cluster_size = 0
        if config.FEATURE_SNIPER_DBSCAN:
            try:
                trades_nearby = await self.db.get_trades_for_sniper_scan(
                    trade.market_id, trade.side, trade.outcome,
                    window_minutes=config.SNIPER_SCAN_WINDOW_MIN,
                    min_size=config.SNIPER_MIN_TRADE_SIZE,
                )
                if len(trades_nearby) >= config.SNIPER_MIN_CLUSTER_SIZE:
                    # DBSCAN simplificado: agrupar trades por ventana temporal
                    sniper_cluster_size = self._dbscan_cluster(
                        trades_nearby, config.SNIPER_TIME_WINDOW_SEC)
            except Exception:
                pass

        # Analizar con todas las señales (v6.0)
        try:
            candidate = self.analyzer.analyze(
                trade, wallet_stats, market_baseline, cluster,
                accumulation_info=accumulation_info,
                market_price=market_price,
                smart_cluster_count=smart_cluster_count,
                wallet_category_shift=wallet_category_shift,
                cross_basket_count=cross_basket_count,
                sniper_cluster_size=sniper_cluster_size,
            )
        except Exception as e:
            err_msg = f"analyzer: {e} | size=${trade.size} wallet={trade.wallet_address[:12]} market={trade.market_slug}"
            print(f"ERROR {err_msg}", flush=True)
            self._debug["errors"] = self._debug.get("errors", 0) + 1
            self._debug["last_error"] = err_msg
            return

        # Copy-trade: si wallet está en watchlist, alertar aunque score sea bajo
        is_copy = trade.wallet_address.lower() in self._watchlist
        should_alert = self.analyzer.should_alert(candidate) or is_copy

        self._debug["scored"] += 1
        self._debug["last_scores"] = (self._debug["last_scores"] + [{
            "score": candidate.score, "size": trade.size,
            "triggers": candidate.triggers[:3], "market": (trade.market_question or "")[:50],
            "slug": (trade.market_slug or "")[:40], "cat": trade.market_category or "",
        }])[-20:]

        if not should_alert:
            self._debug["low_score"] += 1
            return

        # Filtrar por categorías excluidas (cacheadas en memoria)
        if self._excluded_categories:
            excluded_cats = self._excluded_categories
            market_cat = (trade.market_category or "").lower()
            if market_cat and market_cat in excluded_cats:
                self._debug["excluded_cat"] += 1
                print(f"DEBUG EXCLUDED cat={market_cat} slug={trade.market_slug} score={candidate.score}", flush=True)
                return
            market_slug_lower = (trade.market_slug or "").lower()
            matched_slug = [cat for cat in excluded_cats if len(cat) > 2 and cat in market_slug_lower]
            if matched_slug:
                self._debug["excluded_cat"] += 1
                print(f"DEBUG EXCLUDED slug_match={matched_slug} slug={trade.market_slug} score={candidate.score}", flush=True)
                return
            q_lower = (trade.market_question or "").lower()
            sport_keywords = {"nba", "nfl", "nhl", "mlb", "mls", "soccer", "football", "basketball", "baseball", "hockey", "esports"}
            matched_keywords = excluded_cats & sport_keywords
            if any(kw in q_lower for kw in matched_keywords):
                self._debug["excluded_cat"] += 1
                print(f"DEBUG EXCLUDED keyword in question: {trade.market_question[:60]} score={candidate.score}", flush=True)
                return

        # Cooldown
        if not await self.db.should_alert(trade.wallet_address, trade.market_id, config.COOLDOWN_HOURS):
            self._debug["cooldown"] += 1
            print(f"DEBUG COOLDOWN wallet={trade.wallet_address[:10]} market={trade.market_slug} score={candidate.score}", flush=True)
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

    # ── Sniper DBSCAN ──────────────────────────────────────────────────

    def _dbscan_cluster(self, trades: list[dict], time_window_sec: float) -> int:
        """DBSCAN simplificado: encontrar el cluster más grande de trades
        donde cada trade está a menos de time_window_sec del siguiente.
        Retorna el tamaño del cluster más grande (wallets únicas).
        """
        if not trades:
            return 0
        # Extraer timestamps y wallets
        points = [(float(t["ts_epoch"]), t["wallet_address"]) for t in trades if t.get("ts_epoch")]
        if len(points) < 2:
            return 0
        points.sort(key=lambda x: x[0])
        # Encontrar clusters de trades cercanos en tiempo
        clusters = []
        current_cluster = {points[0][1]}
        last_ts = points[0][0]
        for i in range(1, len(points)):
            ts, wallet = points[i]
            if ts - last_ts <= time_window_sec:
                current_cluster.add(wallet)
            else:
                if len(current_cluster) >= 2:
                    clusters.append(current_cluster)
                current_cluster = {wallet}
            last_ts = ts
        if len(current_cluster) >= 2:
            clusters.append(current_cluster)
        if not clusters:
            return 0
        return max(len(c) for c in clusters)

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

    # ── Crypto Arb Signal Processing ────────────────────────────────

    async def process_crypto_signals(self):
        """Procesar señales del detector crypto y guardarlas en DB + Telegram."""
        if not self.crypto_detector:
            return
        try:
            signals = self.crypto_detector.get_recent_signals(200)
            if not signals:
                return
            # Cargar condition_ids ya guardados (una sola query)
            existing = await self.db.get_crypto_signals_history(limit=500)
            existing_cids = {e["condition_id"] for e in existing}

            saved = 0
            for sig in signals:
                if sig["condition_id"] in existing_cids:
                    continue

                # Guardar en DB
                sig["paper_bet_size"] = config.CRYPTO_ARB_PAPER_BET
                await self.db.record_crypto_signal(sig)
                existing_cids.add(sig["condition_id"])
                saved += 1

                # Enviar a Telegram
                if config.CRYPTO_ARB_TELEGRAM:
                    await self.notifier.send_crypto_signal(sig)

            if saved:
                print(f"Crypto signals guardadas: {saved}", flush=True)
        except Exception as e:
            print(f"Error procesando crypto signals: {e}", flush=True)

    async def resolve_crypto_signals(self):
        """Resolver señales crypto pendientes.

        CLOB API tarda horas en marcar mercados 5-min como cerrados.
        Usamos Gamma API por slug (rápido) o auto-resolución por precio Binance.
        """
        try:
            unresolved = await self.db.get_unresolved_crypto_signals()
            if not unresolved:
                return

            now = datetime.now()
            resolved_count = 0

            async with httpx.AsyncClient(timeout=15) as client:
                for sig in unresolved:
                    cid = sig["condition_id"]
                    direction = sig.get("direction", "").lower()
                    coin = sig.get("coin", "BTC")
                    created = sig.get("created_at")
                    time_rem = sig.get("time_remaining_sec", 300)

                    # Calcular cuándo cerró el mercado
                    if not created:
                        continue
                    time_rem = time_rem or 0  # Asegurar que 0 no sea None
                    if isinstance(created, str):
                        try:
                            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        except Exception:
                            continue
                    end_time = created + timedelta(seconds=int(time_rem))
                    now = datetime.now(end_time.tzinfo)
                    # Si el mercado aún no cerró, saltar
                    if now < end_time:
                        continue
                    secs_since_close = (now - end_time).total_seconds()

                    # Si cerró hace menos de 20s, esperar un poco para que se actualice
                    if secs_since_close < 20:
                        continue

                    resolution = None

                    # Método 1: Gamma API por slug del evento
                    # Primero usar el event_slug guardado, luego recalcular
                    event_slug = sig.get("event_slug", "")
                    slugs_to_try = []
                    if event_slug:
                        slugs_to_try.append(event_slug)
                    # Fallback: recalcular slugs posibles
                    end_ts = int(end_time.timestamp())
                    coin_prefix = coin.lower()
                    for dur, interval in [("5m", 300), ("15m", 900)]:
                        start_ts = end_ts - interval
                        start_ts = (start_ts // interval) * interval
                        calc_slug = f"{coin_prefix}-updown-{dur}-{start_ts}"
                        if calc_slug not in slugs_to_try:
                            slugs_to_try.append(calc_slug)
                        # También probar +/- 1 intervalo por si hay desfase
                        for adj in [-interval, interval]:
                            adj_slug = f"{coin_prefix}-updown-{dur}-{start_ts + adj}"
                            if adj_slug not in slugs_to_try:
                                slugs_to_try.append(adj_slug)

                    try:
                        for slug in slugs_to_try:
                            resp = await client.get(
                                f"{config.GAMMA_API_URL}/events",
                                params={"slug": slug},
                            )
                            if resp.status_code != 200:
                                continue
                            events = resp.json()
                            if not events:
                                continue
                            ev = events[0]
                            for m in ev.get("markets", []):
                                m_cid = m.get("conditionId", "")
                                if m_cid == cid and m.get("closed"):
                                    prices = m.get("outcomePrices", "")
                                    if isinstance(prices, str):
                                        try:
                                            prices = __import__("json").loads(prices)
                                        except Exception:
                                            prices = []
                                    outcomes = m.get("outcomes", "")
                                    if isinstance(outcomes, str):
                                        try:
                                            outcomes = __import__("json").loads(outcomes)
                                        except Exception:
                                            outcomes = []
                                    if prices and outcomes:
                                        for i, p in enumerate(prices):
                                            if float(p) >= 0.95 and i < len(outcomes):
                                                resolution = outcomes[i]
                                                break
                            if resolution:
                                break
                    except Exception as e:
                        print(f"[CryptoResolve] Gamma error para {cid[:12]}: {e}", flush=True)

                    # Método 2: CLOB API (puede funcionar para mercados más viejos)
                    if not resolution:
                        try:
                            resp2 = await client.get(
                                f"{config.CLOB_API_URL}/markets/{cid}"
                            )
                            if resp2.status_code == 200:
                                data = resp2.json()
                                if data.get("closed"):
                                    for tok in data.get("tokens", []):
                                        if tok.get("winner") is True:
                                            resolution = tok.get("outcome", "")
                                            break
                        except Exception:
                            pass

                    # Método 3 ELIMINADO: Binance auto-resolve era incorrecto.
                    # Polymarket compara contra "price to beat" (inicio del mercado),
                    # no contra el spot_price cuando se detectó la señal.
                    # Solo confiamos en la resolución oficial de Gamma/CLOB.

                    if not resolution:
                        continue

                    # Calcular resultado
                    won = resolution.lower() == direction
                    paper_result = "win" if won else "loss"
                    bet = float(sig.get("paper_bet_size", config.CRYPTO_ARB_PAPER_BET))
                    odds = float(sig.get("poly_odds", 0.5))
                    paper_pnl = bet * (1 - odds) if won else -(bet * odds)

                    await self.db.resolve_crypto_signal(
                        sig["id"], resolution, paper_result, round(paper_pnl, 2)
                    )
                    resolved_count += 1

                    if self.crypto_detector:
                        self.crypto_detector.resolve_signal(cid, paper_result, round(paper_pnl, 2))

                    print(f"[CryptoResolve] {coin} {direction} → {resolution} = {paper_result} "
                          f"(${paper_pnl:.2f})", flush=True)

            if resolved_count:
                print(f"Crypto signals resueltas: {resolved_count}", flush=True)
        except Exception as e:
            print(f"Error resolviendo crypto signals: {e}", flush=True)

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
