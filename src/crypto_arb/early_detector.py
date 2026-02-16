"""Detector de entrada temprana para mercados crypto up/down de Polymarket.

Monitorea mercados ANTES de que abran (eventStartTime) para detectar
momentum fuerte y generar señales de compra inmediata al segundo 0,
cuando los odds todavía están en ~0.50.

Strategy: "early_entry"
"""
import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog
import httpx

from src import config
from src.crypto_arb.binance_feed import BinanceFeed
from src.crypto_arb.detector import CryptoSignal, PAIR_MAP

logger = structlog.get_logger()

GAMMA_API_URL = getattr(config, 'GAMMA_API_URL', 'https://gamma-api.polymarket.com')
CLOB_API_URL = getattr(config, 'CLOB_API_URL', 'https://clob.polymarket.com')


class WatchedMarket:
    """Un mercado que estamos monitoreando pre-apertura."""
    __slots__ = (
        "coin", "slug", "interval", "event_start_ts", "end_ts",
        "condition_id", "question", "tokens", "event_slug",
        "state", "momentum_at_start", "target_price",
        "signal_generated", "created_at",
    )

    def __init__(self, coin, slug, interval, event_start_ts, end_ts,
                 condition_id, question, tokens, event_slug):
        self.coin = coin
        self.slug = slug
        self.interval = interval
        self.event_start_ts = event_start_ts
        self.end_ts = end_ts
        self.condition_id = condition_id
        self.question = question
        self.tokens = tokens
        self.event_slug = event_slug
        self.state = "upcoming"  # upcoming → watching → active → signal_sent → expired
        self.momentum_at_start = None
        self.target_price = None
        self.signal_generated = False
        self.created_at = time.time()


class EarlyEntryDetector:
    """Detecta oportunidades de entrada temprana en mercados crypto."""

    def __init__(self, binance_feed: BinanceFeed):
        self.feed = binance_feed
        self._watched: dict[str, WatchedMarket] = {}  # slug → WatchedMarket
        self._signals_today: list[CryptoSignal] = []
        self._signals_today_date: str = ""
        self._running = False
        self._last_scan = 0.0
        # Config (sobreescribible desde DB/dashboard)
        self.pre_monitor_sec = 120
        self.entry_window_sec = 15
        self.min_momentum_pct = 0.10
        self.enabled = False

    def configure(self, cfg: dict):
        """Actualizar configuración desde DB/dashboard."""
        self.enabled = cfg.get("early_entry_enabled", False)
        self.pre_monitor_sec = int(cfg.get("early_entry_pre_monitor", 120))
        self.entry_window_sec = int(cfg.get("early_entry_window", 15))
        self.min_momentum_pct = float(cfg.get("early_entry_min_momentum", 0.10))

    async def start(self):
        """Loop principal."""
        self._running = True
        self._loop_count = 0
        logger.info("early_entry_detector_started")
        print(f"[EarlyEntry] Detector iniciado (enabled={self.enabled})", flush=True)

        while self._running:
            try:
                now = time.time()
                self._loop_count += 1

                # Log periódico cada 60 iteraciones (~2 min)
                if self._loop_count % 60 == 1:
                    print(f"[EarlyEntry] Loop #{self._loop_count} enabled={self.enabled} "
                          f"watching={len(self._watched)} "
                          f"pre_monitor={self.pre_monitor_sec}s", flush=True)

                # Escanear mercados cada 30s
                if now - self._last_scan > 30:
                    await self._scan_upcoming_markets()
                    self._last_scan = now

                # Evaluar mercados cada 2 segundos
                await self._evaluate_markets()

            except Exception as e:
                print(f"[EarlyEntry] Error: {e}", flush=True)

            await asyncio.sleep(2)

    async def stop(self):
        self._running = False

    async def _scan_upcoming_markets(self):
        """Agregar mercados predichos basados en el patrón de timestamps.

        Polymarket NO crea los eventos hasta que empiezan, así que no podemos
        buscarlos en Gamma API por adelantado. En cambio, creamos entradas
        "predichas" basadas en el patrón conocido de slugs y timestamps.
        Solo consultamos Gamma/CLOB cuando el mercado está por abrir.
        """
        if not self.enabled:
            return

        try:
            enabled_coins = {c["symbol"] for c in config.CRYPTO_ARB_COINS}
            now_ts = int(time.time())

            slug_templates = []
            if "BTC" in enabled_coins:
                slug_templates.append(("BTC", "btc-updown-5m", 300))
                slug_templates.append(("BTC", "btc-updown-15m", 900))
            if "ETH" in enabled_coins:
                slug_templates.append(("ETH", "eth-updown-15m", 900))
            if "SOL" in enabled_coins:
                slug_templates.append(("SOL", "sol-updown-15m", 900))
            if "XRP" in enabled_coins:
                slug_templates.append(("XRP", "xrp-updown-15m", 900))

            new_found = 0
            for coin, prefix, interval in slug_templates:
                base_ts = (now_ts // interval) * interval
                # Generar slugs para el período actual y los próximos 3
                for offset in range(0, 4):
                    event_start_ts = base_ts + (offset * interval)
                    slug = f"{prefix}-{event_start_ts}"

                    if slug in self._watched:
                        continue

                    time_until_start = event_start_ts - now_ts
                    # Ventana de descubrimiento: al menos un intervalo completo
                    scan_window = max(self.pre_monitor_sec, interval)
                    if time_until_start > scan_window:
                        continue
                    if time_until_start < -interval:
                        continue

                    # Crear mercado PREDICHO sin consultar Gamma API
                    # (Polymarket no crea eventos hasta que empiezan)
                    dur_label = f"{interval // 60}m"
                    question = f"Will {coin} go up or down in the next {dur_label}?"

                    wm = WatchedMarket(
                        coin=coin,
                        slug=slug,
                        interval=interval,
                        event_start_ts=event_start_ts,
                        end_ts=event_start_ts + interval,
                        condition_id="",  # Se llena cuando Gamma tenga el evento
                        question=question,
                        tokens=[],
                        event_slug=slug,
                    )

                    if time_until_start > self.pre_monitor_sec:
                        wm.state = "upcoming"
                    elif time_until_start > 0:
                        wm.state = "watching"
                    else:
                        wm.state = "active"

                    self._watched[slug] = wm
                    new_found += 1

            # Para mercados que están por abrir o activos, intentar obtener
            # condition_id y tokens desde Gamma/CLOB (si aún no los tienen)
            await self._resolve_market_ids()

            # Limpiar mercados expirados
            expired = [s for s, wm in self._watched.items()
                       if time.time() > wm.end_ts + 60]
            for s in expired:
                del self._watched[s]

            if new_found:
                total = len(self._watched)
                upcoming = sum(1 for wm in self._watched.values() if wm.state == "upcoming")
                watching = sum(1 for wm in self._watched.values() if wm.state == "watching")
                print(f"[EarlyEntry] Scan: +{new_found} nuevos, "
                      f"{total} total ({upcoming} upcoming, {watching} watching)",
                      flush=True)

        except Exception as e:
            print(f"[EarlyEntry] Scan error: {e}", flush=True)

    async def _resolve_market_ids(self):
        """Para mercados cerca de abrir o activos, obtener condition_id desde Gamma."""
        now_ts = time.time()
        to_resolve = [
            wm for wm in self._watched.values()
            if not wm.condition_id
            and (wm.event_start_ts - now_ts) < 90  # Resolver hasta 90s antes de apertura
            and wm.state in ("upcoming", "watching", "active")
        ]
        if not to_resolve:
            return

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for wm in to_resolve:
                    try:
                        resp = await client.get(
                            f"{GAMMA_API_URL}/events",
                            params={"slug": wm.slug},
                        )
                        if resp.status_code != 200:
                            continue
                        events = resp.json()
                        if not events:
                            continue

                        ev = events[0]
                        for m in ev.get("markets", []):
                            cid = m.get("conditionId", "")
                            if cid and not m.get("closed"):
                                wm.condition_id = cid
                                wm.question = m.get("question", "") or ev.get("title", wm.question)
                                # Obtener tokens
                                try:
                                    resp2 = await client.get(f"{CLOB_API_URL}/markets/{cid}")
                                    if resp2.status_code == 200:
                                        wm.tokens = resp2.json().get("tokens", [])
                                except Exception:
                                    pass
                                break
                    except Exception:
                        pass
        except Exception as e:
            print(f"[EarlyEntry] Resolve IDs error: {e}", flush=True)

    async def _evaluate_markets(self):
        """Evaluar todos los mercados monitoreados y generar señales."""
        if not self.enabled:
            return

        now_ts = time.time()

        for slug, wm in list(self._watched.items()):
            if wm.signal_generated:
                continue

            time_until_start = wm.event_start_ts - now_ts
            time_since_start = now_ts - wm.event_start_ts

            pair = PAIR_MAP.get(wm.coin)
            if not pair:
                continue

            # Actualizar estado
            if time_until_start > self.pre_monitor_sec:
                wm.state = "upcoming"  # Encontrado pero fuera de ventana de monitoreo
                continue
            elif time_until_start > 0:
                wm.state = "watching"  # Dentro de ventana, monitoreando momentum
            elif time_since_start <= self.entry_window_sec:
                wm.state = "active"   # Mercado abierto, ventana de entrada
            else:
                wm.state = "expired"
                continue

            # --- WATCHING: acumular datos de momentum ---
            if wm.state == "watching":
                momentum = self.feed.get_momentum(pair, 60)
                if momentum:
                    wm.momentum_at_start = momentum
                continue

            # --- ACTIVE: mercado acaba de abrir, evaluar entrada ---
            if wm.state == "active":
                signal = await self._try_early_signal(wm, pair, time_since_start)
                if signal:
                    self._record_signal(signal)
                    wm.signal_generated = True
                    wm.state = "signal_sent"

    async def _try_early_signal(self, wm: WatchedMarket, pair: str,
                                 time_since_start: float) -> Optional[CryptoSignal]:
        """Intentar generar señal early entry para un mercado activo."""
        now_ts = time.time()

        # Si no tenemos condition_id, intentar resolver ahora
        if not wm.condition_id:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(
                        f"{GAMMA_API_URL}/events",
                        params={"slug": wm.slug},
                    )
                    if resp.status_code == 200:
                        events = resp.json()
                        if events:
                            for m in events[0].get("markets", []):
                                cid = m.get("conditionId", "")
                                if cid and not m.get("closed"):
                                    wm.condition_id = cid
                                    wm.question = m.get("question", "") or events[0].get("title", wm.question)
                                    try:
                                        resp2 = await client.get(f"{CLOB_API_URL}/markets/{cid}")
                                        if resp2.status_code == 200:
                                            wm.tokens = resp2.json().get("tokens", [])
                                    except Exception:
                                        pass
                                    break
            except Exception:
                pass

        if not wm.condition_id:
            print(f"[EarlyEntry] {wm.coin} {wm.slug}: sin condition_id, no se puede generar señal", flush=True)
            return None

        # Registrar target price
        if not wm.target_price:
            local_price = self.feed.get_price_at_time(
                pair, float(wm.event_start_ts), tolerance_sec=5.0)
            if local_price:
                wm.target_price = local_price
            else:
                wm.target_price = self.feed.get_price(pair)

        if not wm.target_price:
            return None

        # Momentum pre-start (últimos 60-120s)
        momentum = self.feed.get_momentum(pair, min(self.pre_monitor_sec, 120))
        if not momentum:
            return None

        change_pct = abs(momentum["change_pct"])
        direction = momentum["direction"]

        if change_pct < self.min_momentum_pct:
            return None

        # Consistencia de tendencia
        trend = self.feed.get_trend_consistency(pair, 60)
        if not trend or trend["direction"] != direction:
            return None
        if trend["consistency"] < 0.55:
            return None

        # Precio actual confirma dirección vs target
        current_price = self.feed.get_price(pair)
        if not current_price:
            return None

        price_diff = current_price - wm.target_price
        price_direction = "up" if price_diff > 0 else "down"

        # Momentum y dirección vs target deben coincidir (o diff muy pequeño)
        if price_direction != direction:
            if abs(price_diff / wm.target_price * 100) > 0.02:
                return None

        # Refrescar odds
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{CLOB_API_URL}/markets/{wm.condition_id}")
                if resp.status_code == 200:
                    wm.tokens = resp.json().get("tokens", [])
        except Exception:
            pass

        up_odds = down_odds = None
        for tok in wm.tokens:
            outcome = tok.get("outcome", "").lower()
            price = float(tok.get("price", 0.5))
            if outcome == "up":
                up_odds = price
            elif outcome == "down":
                down_odds = price

        if up_odds is None or down_odds is None:
            return None

        poly_odds = up_odds if direction == "up" else down_odds

        # Calcular score early entry
        atr = self.feed.get_atr(pair, window_sec=300)
        if not atr or atr <= 0:
            atr = abs(current_price * 0.001)

        distance_atr = abs(price_diff) / atr if atr > 0 else 0

        score = change_pct * 10
        score *= (1 + trend["consistency"])
        if distance_atr > 0.3:
            score *= 1.2

        # Indicadores
        vwap_aligned = False
        if config.FEATURE_VWAP:
            vwap_data = self.feed.get_vwap(pair, 300)
            if vwap_data:
                if (direction == "up" and vwap_data["above_vwap"]) or \
                   (direction == "down" and not vwap_data["above_vwap"]):
                    vwap_aligned = True
                    score *= 1.1

        rsi_aligned = False
        if config.FEATURE_RSI:
            rsi_data = self.feed.get_rsi(pair, config.RSI_PERIOD, config.RSI_CANDLE_SEC)
            if rsi_data:
                if (direction == "up" and rsi_data["rsi"] < config.RSI_OVERSOLD) or \
                   (direction == "down" and rsi_data["rsi"] > config.RSI_OVERBOUGHT):
                    rsi_aligned = True
                    score *= 1.1

        macd_aligned = False
        if config.FEATURE_MACD:
            macd_data = self.feed.get_macd(
                pair, config.MACD_FAST, config.MACD_SLOW,
                config.MACD_SIGNAL, config.MACD_CANDLE_SEC)
            if macd_data:
                if (direction == "up" and macd_data["bullish"]) or \
                   (direction == "down" and macd_data["bearish"]):
                    macd_aligned = True
                    score *= 1.1

        fair_odds = min(0.55 + score * 0.15, 0.90)
        edge_pct = (fair_odds - poly_odds) * 100
        confidence = min(score * 50, 100)

        time_remaining = wm.end_ts - now_ts

        score_details = {
            "price_to_beat": round(wm.target_price, 2),
            "current_price": round(current_price, 2),
            "price_diff": round(price_diff, 2),
            "momentum_pct": round(change_pct, 4),
            "trend_consistency": round(trend["consistency"], 3),
            "trend_aligned": True,
            "distance_atr": round(distance_atr, 3),
            "consistency_factor": round(trend["consistency"], 3),
            "atr": round(atr, 2),
            "score": round(score, 3),
            "rsi_aligned": rsi_aligned,
            "macd_aligned": macd_aligned,
            "vwap_aligned": vwap_aligned,
            "entry_delay_sec": round(time_since_start, 1),
            "time_factor": 0.0,
        }

        end_date = datetime.fromtimestamp(wm.end_ts, tz=timezone.utc)

        signal = CryptoSignal(
            coin=wm.coin,
            direction=direction,
            spot_change_pct=round(change_pct, 4),
            poly_odds=poly_odds,
            fair_odds=fair_odds,
            confidence=confidence,
            edge_pct=edge_pct,
            condition_id=wm.condition_id,
            market_question=wm.question,
            spot_price=current_price,
            time_remaining_sec=int(time_remaining),
            end_date=end_date,
            event_slug=wm.event_slug,
            strategy="early_entry",
            score_details=score_details,
        )

        print(f"[EarlyEntry] ⚡ SEÑAL {wm.coin} {direction.upper()} | "
              f"score={score:.2f} momentum={change_pct:.3f}% "
              f"odds={poly_odds:.2f} edge={edge_pct:.1f}% "
              f"delay={time_since_start:.1f}s", flush=True)

        return signal

    def _record_signal(self, signal: CryptoSignal):
        """Registrar señal early entry."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._signals_today_date != today:
            self._signals_today = []
            self._signals_today_date = today

        for s in self._signals_today:
            if s.condition_id == signal.condition_id:
                return

        self._signals_today.append(signal)

    def get_recent_signals(self, limit: int = 50) -> list[dict]:
        """Señales early entry recientes para dashboard y autotrader."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=30)
        result = []
        for s in reversed(self._signals_today[-limit * 2:]):
            if s.timestamp < cutoff and s.resolved:
                continue
            if s.end_date:
                remaining = max(0, int((s.end_date - now).total_seconds()))
            else:
                remaining = max(0, s.time_remaining_sec -
                                int((now - s.timestamp).total_seconds()))
            result.append({
                "coin": s.coin,
                "direction": s.direction,
                "spot_change_pct": s.spot_change_pct,
                "poly_odds": s.poly_odds,
                "fair_odds": s.fair_odds,
                "confidence": s.confidence,
                "edge_pct": s.edge_pct,
                "condition_id": s.condition_id,
                "market_question": s.market_question,
                "spot_price": s.spot_price,
                "time_remaining_sec": remaining,
                "expected_profit_pct": s.expected_profit_pct,
                "timestamp": s.timestamp.isoformat(),
                "event_slug": s.event_slug,
                "polymarket_url": (f"https://polymarket.com/event/{s.event_slug}"
                                   if s.event_slug else ""),
                "strategy": s.strategy,
                "score_details": s.score_details,
                "resolved": s.resolved,
                "result": s.result,
                "paper_pnl": s.paper_pnl,
            })
            if len(result) >= limit:
                break
        return result

    def get_watching_markets(self) -> list[dict]:
        """Mercados en observación para el dashboard."""
        now_ts = time.time()
        result = []
        for slug, wm in self._watched.items():
            if wm.state == "expired":
                continue
            pair = PAIR_MAP.get(wm.coin, "")
            momentum = self.feed.get_momentum(pair, 60) if pair else None

            time_until_start = wm.event_start_ts - now_ts

            result.append({
                "slug": slug,
                "coin": wm.coin,
                "interval": wm.interval,
                "event_start_ts": wm.event_start_ts,
                "time_until_start": round(time_until_start, 0),
                "seconds_to_start": max(0, round(time_until_start, 0)),
                "state": wm.state,
                "momentum_pct": round(abs(momentum["change_pct"]), 4) if momentum else 0,
                "momentum_direction": momentum["direction"] if momentum else "--",
                "target_price": wm.target_price,
                "signal_generated": wm.signal_generated,
                "condition_id": wm.condition_id,
                "question": wm.question,
            })

        return sorted(result, key=lambda x: x["time_until_start"])

    def resolve_signal(self, condition_id: str, result: str, pnl: float):
        """Marcar señal como resuelta."""
        for s in self._signals_today:
            if s.condition_id == condition_id and not s.resolved:
                s.resolved = True
                s.result = result
                s.paper_pnl = pnl
