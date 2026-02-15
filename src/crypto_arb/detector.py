"""Detector de divergencia entre precio spot (Binance) y odds de Polymarket.

Busca mercados de crypto 15-min up/down donde el precio spot ya se movió
pero Polymarket todavía no ajustó las odds.
"""
import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog
import httpx

from src import config
from src.crypto_arb.binance_feed import BinanceFeed

logger = structlog.get_logger()


@dataclass
class CryptoSignal:
    """Una señal de arbitraje detectada."""
    coin: str                    # "BTC", "ETH", "SOL"
    direction: str               # "up" o "down"
    spot_change_pct: float       # Cambio % en spot
    poly_odds: float             # Odds actuales en Polymarket (0-1)
    fair_odds: float             # Odds estimadas reales (0-1)
    confidence: float            # Confianza 0-100
    edge_pct: float              # Edge estimado en %
    condition_id: str            # ID del mercado en Polymarket
    market_question: str         # Pregunta del mercado
    spot_price: float            # Precio spot actual
    time_remaining_sec: int      # Segundos hasta cierre
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_date: Optional[datetime] = None  # Fecha de cierre del mercado
    event_slug: str = ""         # Slug del evento en Polymarket
    strategy: str = ""           # "divergence" o "score"
    score_details: dict = field(default_factory=dict)  # Detalles del score
    resolved: bool = False       # Si ya se resolvió
    result: Optional[str] = None # "win" o "loss"
    paper_pnl: float = 0.0      # PnL simulado

    @property
    def expected_profit_pct(self) -> float:
        """Profit esperado si compramos a poly_odds y gana."""
        if self.poly_odds <= 0:
            return 0
        return ((1.0 / self.poly_odds) - 1) * 100


# Regex para detectar mercados crypto up/down
# Patrones posibles:
#   "Will Bitcoin go up or down..." / "Bitcoin price up or down..."
#   "BTC above $X at 3:00 PM" / "Bitcoin Up or Down by 3:15 PM"
#   "Will the price of Bitcoin..." / "Bitcoin 15-Minute..."
CRYPTO_MARKET_RE = re.compile(
    r"\b(Bitcoin|BTC|Ethereum|ETH|Solana|SOL)\b",
    re.IGNORECASE,
)
# Segundo filtro: debe ser un mercado de tipo up/down o precio
UPDOWN_RE = re.compile(
    r"(up\s+or\s+down|above|below|higher|lower|price|15.?min)",
    re.IGNORECASE,
)

COIN_MAP = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL",
    "xrp": "XRP", "ripple": "XRP",
}

PAIR_MAP = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
    "XRP": "xrpusdt",
}


class CryptoArbDetector:
    """Detecta oportunidades de arbitraje en mercados crypto up/down de Polymarket."""

    def __init__(self, binance_feed: BinanceFeed):
        self.feed = binance_feed
        self._active_markets: dict[str, dict] = {}  # condition_id -> market_data
        self._last_scan = 0.0
        self._last_token_refresh = 0.0
        self._signals_today: list[CryptoSignal] = []
        self._signals_today_date: str = ""
        self._running = False

    async def start(self):
        """Loop principal: escanear mercados y detectar divergencias."""
        self._running = True
        logger.info("crypto_arb_detector_started")

        while self._running:
            try:
                # Refrescar mercados activos cada 60 segundos
                now = time.time()
                if now - self._last_scan > 60:
                    await self._scan_active_markets()
                    self._last_scan = now
                    self._last_token_refresh = now

                # Refrescar solo token prices (odds) cada 15 segundos
                if now - self._last_token_refresh > 15 and self._active_markets:
                    await self._refresh_token_prices()
                    self._last_token_refresh = now

                # Buscar señales cada 3 segundos según estrategia activa
                if config.CRYPTO_ARB_STRATEGY == "divergence":
                    signals = await self._check_divergences()
                else:
                    signals = await self._check_score_strategy()
                for signal in signals:
                    self._record_signal(signal)

            except Exception as e:
                logger.warning("crypto_arb_error", error=str(e))

            await asyncio.sleep(3)

    async def stop(self):
        self._running = False

    async def _scan_active_markets(self):
        """Buscar mercados crypto up/down activos en Polymarket.

        Polymarket crea mercados con slugs predecibles basados en timestamps:
          - btc-updown-5m-{unix_ts}   (cada 5 min)
          - btc-updown-15m-{unix_ts}  (cada 15 min)
          - eth-updown-15m-{unix_ts}  (cada 15 min)
          - sol-updown-15m-{unix_ts}  (cada 15 min)

        El API genérico de Gamma NO devuelve estos mercados en las primeras
        páginas, así que los buscamos por slug exacto calculando timestamps.
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                enabled_coins = {c["symbol"] for c in config.CRYPTO_ARB_COINS}
                now_ts = int(time.time())

                # Generar slugs candidatos para ventanas recientes y futuras
                # BTC tiene 5m y 15m, ETH/SOL solo 15m
                slug_templates = []
                if "BTC" in enabled_coins:
                    slug_templates.append(("BTC", "btc-updown-5m", 300))   # cada 5 min
                    slug_templates.append(("BTC", "btc-updown-15m", 900))  # cada 15 min
                    slug_templates.append(("BTC", "btc-updown-1h", 3600))  # cada 1 hora
                if "ETH" in enabled_coins:
                    slug_templates.append(("ETH", "eth-updown-15m", 900))
                    slug_templates.append(("ETH", "eth-updown-1h", 3600))
                if "SOL" in enabled_coins:
                    slug_templates.append(("SOL", "sol-updown-15m", 900))
                    slug_templates.append(("SOL", "sol-updown-1h", 3600))
                if "XRP" in enabled_coins:
                    slug_templates.append(("XRP", "xrp-updown-15m", 900))
                    slug_templates.append(("XRP", "xrp-updown-1h", 3600))

                new_active = {}
                slugs_checked = 0

                for coin, prefix, interval in slug_templates:
                    # Redondear timestamp al intervalo más cercano
                    base_ts = (now_ts // interval) * interval
                    # Buscar desde -2 intervalos hasta +6 intervalos adelante
                    for offset in range(-2, 7):
                        ts = base_ts + (offset * interval)
                        slug = f"{prefix}-{ts}"
                        slugs_checked += 1

                        try:
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
                            if ev.get("closed") or not ev.get("active"):
                                continue

                            # Extraer mercados activos del evento
                            for m in ev.get("markets", []):
                                if m.get("closed"):
                                    continue
                                cid = m.get("conditionId", "")
                                if not cid or cid in new_active:
                                    continue

                                end_str = m.get("endDate") or ev.get("endDate")
                                end_date = None
                                if end_str:
                                    try:
                                        ed = datetime.fromisoformat(
                                            end_str.replace("Z", "+00:00"))
                                        if ed.tzinfo is None:
                                            ed = ed.replace(tzinfo=timezone.utc)
                                        end_date = ed
                                    except Exception:
                                        pass

                                new_active[cid] = {
                                    "condition_id": cid,
                                    "question": m.get("question", "") or ev.get("title", ""),
                                    "coin": coin,
                                    "end_date": end_date,
                                    "event_slug": slug,
                                    "open_ts": ts,       # Timestamp de apertura del mercado
                                    "interval": interval, # Duración en segundos (300 o 900)
                                    "tokens": [],
                                    "price_to_beat": None, # Se llena después
                                }
                        except Exception:
                            pass

                # Obtener tokens (precios) de CLOB para cada mercado activo
                for cid, mdata in new_active.items():
                    try:
                        resp2 = await client.get(
                            f"{config.CLOB_API_URL}/markets/{cid}"
                        )
                        if resp2.status_code == 200:
                            clob = resp2.json()
                            mdata["tokens"] = clob.get("tokens", [])
                    except Exception:
                        pass

                # Obtener price_to_beat para cada mercado (Binance klines o historia local)
                for cid, mdata in new_active.items():
                    if mdata.get("price_to_beat"):
                        continue
                    pair = PAIR_MAP.get(mdata["coin"], "")
                    open_ts = mdata.get("open_ts", 0)
                    if not pair or not open_ts:
                        continue
                    # Intento 1: historia local de ticks
                    local_price = self.feed.get_price_at_time(pair, float(open_ts), tolerance_sec=10.0)
                    if local_price:
                        mdata["price_to_beat"] = local_price
                        continue
                    # Intento 2: Binance klines API (1 vela de 1 minuto)
                    try:
                        resp3 = await client.get(
                            "https://api.binance.com/api/v3/klines",
                            params={
                                "symbol": pair.upper(),
                                "interval": "1m",
                                "startTime": int(open_ts * 1000),
                                "limit": 1,
                            },
                        )
                        if resp3.status_code == 200:
                            klines = resp3.json()
                            if klines:
                                mdata["price_to_beat"] = float(klines[0][1])  # Open price
                    except Exception:
                        pass

                self._active_markets = new_active

                print(f"[CryptoDetector] Scan: {slugs_checked} slugs verificados, "
                      f"{len(new_active)} crypto up/down activos", flush=True)
                for cid, md in list(new_active.items())[:8]:
                    remaining = ""
                    if md["end_date"]:
                        rem_sec = (md["end_date"] - datetime.now(timezone.utc)).total_seconds()
                        remaining = f" ({rem_sec/60:.0f}min)"
                    print(f"  → {md['coin']}: {md['question'][:70]}{remaining}", flush=True)

        except Exception as e:
            print(f"[CryptoDetector] Scan error: {e}", flush=True)
            import traceback
            traceback.print_exc()

    async def _refresh_token_prices(self):
        """Refrescar solo los token prices (odds) de mercados activos via CLOB.
        Más ligero que _scan_active_markets — solo actualiza precios, no busca nuevos mercados.
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for cid, mdata in self._active_markets.items():
                    try:
                        resp = await client.get(f"{config.CLOB_API_URL}/markets/{cid}")
                        if resp.status_code == 200:
                            clob = resp.json()
                            mdata["tokens"] = clob.get("tokens", [])
                    except Exception:
                        pass
        except Exception as e:
            print(f"[CryptoDetector] Token refresh error: {e}", flush=True)

    async def _check_divergences(self) -> list[CryptoSignal]:
        """Comparar precios spot vs odds de Polymarket para encontrar divergencias."""
        signals = []
        now = datetime.now(timezone.utc)

        for cid, mdata in self._active_markets.items():
            coin = mdata["coin"]
            pair = PAIR_MAP.get(coin)
            if not pair:
                continue

            # Verificar tiempo restante
            end_date = mdata.get("end_date")
            if not end_date:
                continue
            try:
                time_remaining = (end_date - now).total_seconds()
            except TypeError:
                continue
            if time_remaining < config.CRYPTO_ARB_MIN_TIME_SEC:
                continue
            if time_remaining > config.CRYPTO_ARB_MAX_TIME_SEC:
                continue

            # Obtener momentum de Binance
            momentum = self.feed.get_momentum(pair, config.CRYPTO_ARB_LOOKBACK_SEC)
            if not momentum:
                continue

            change_pct = abs(momentum["change_pct"])
            direction = momentum["direction"]

            # ¿Movimiento suficiente en spot?
            # Umbral adaptativo: mercados cortos (5m) necesitan menos movimiento
            min_move = config.CRYPTO_ARB_MIN_MOVE_PCT
            question_lower = mdata.get("question", "").lower()
            if "5m" in mdata.get("question", "") or "-5m-" in question_lower or time_remaining <= 360:
                min_move = min(min_move, 0.04)  # 5min: 0.04% es significativo
            elif time_remaining <= 960:
                min_move = min(min_move, 0.08)  # 15min: 0.08%
            if change_pct < min_move:
                continue

            # Obtener odds de Polymarket para el outcome correcto
            tokens = mdata.get("tokens", [])
            up_odds = None
            down_odds = None
            for tok in tokens:
                outcome = tok.get("outcome", "").lower()
                price = float(tok.get("price", 0.5))
                if outcome == "up":
                    up_odds = price
                elif outcome == "down":
                    down_odds = price

            if up_odds is None or down_odds is None:
                continue

            # Determinar si hay divergencia
            if direction == "up":
                poly_odds = up_odds
                target_outcome = "Up"
            else:
                poly_odds = down_odds
                target_outcome = "Down"

            # ¿Odds todavía bajos? (mercado no ajustó)
            if poly_odds > config.CRYPTO_ARB_MAX_POLY_ODDS:
                continue

            # Calcular odds justas estimadas basadas en el momentum
            fair_odds = self._estimate_fair_odds(change_pct, momentum, time_remaining)

            # Edge = diferencia entre fair odds y poly odds
            edge_pct = (fair_odds - poly_odds) * 100

            # Confianza basada en múltiples factores
            confidence = self._calc_confidence(
                change_pct, momentum, poly_odds, fair_odds, time_remaining
            )

            if confidence < config.CRYPTO_ARB_MIN_CONFIDENCE:
                continue

            signal = CryptoSignal(
                coin=coin,
                direction=direction,
                spot_change_pct=momentum["change_pct"],
                poly_odds=poly_odds,
                fair_odds=fair_odds,
                confidence=confidence,
                edge_pct=edge_pct,
                condition_id=cid,
                market_question=mdata["question"],
                spot_price=momentum["last_price"],
                time_remaining_sec=int(time_remaining),
                end_date=mdata.get("end_date"),
                event_slug=mdata.get("event_slug", ""),
                strategy="divergence",
            )
            signals.append(signal)

        return signals

    async def _check_score_strategy(self) -> list[CryptoSignal]:
        """Estrategia score-based: compara precio actual vs price_to_beat,
        normalizado por ATR, consistencia de tendencia y tiempo restante.

        Genera señal solo cuando el score compuesto supera el umbral.
        Diseñada para apostar tarde con alta certeza.
        """
        signals = []
        now = datetime.now(timezone.utc)

        for cid, mdata in self._active_markets.items():
            coin = mdata["coin"]
            pair = PAIR_MAP.get(coin)
            if not pair:
                continue

            # Verificar tiempo restante
            end_date = mdata.get("end_date")
            if not end_date:
                continue
            try:
                time_remaining = (end_date - now).total_seconds()
            except TypeError:
                continue

            # Solo apostar cuando queda poco tiempo (late entry)
            if time_remaining < 10:  # Muy poco, ya casi cerró
                continue
            if time_remaining > config.CRYPTO_ARB_ENTRY_MAX_TIME:
                continue

            # Necesitamos price_to_beat
            price_to_beat = mdata.get("price_to_beat")
            if not price_to_beat or price_to_beat <= 0:
                continue

            # Precio actual de Binance
            current_price = self.feed.get_price(pair)
            if not current_price or current_price <= 0:
                continue

            # Distancia al price_to_beat
            price_diff = current_price - price_to_beat
            direction = "up" if price_diff > 0 else "down"
            abs_diff = abs(price_diff)

            # ATR para normalizar
            interval = mdata.get("interval", 300)
            atr = self.feed.get_atr(pair, window_sec=min(interval, 600))
            if not atr or atr <= 0:
                # Fallback: usar volatilidad del momentum
                momentum = self.feed.get_momentum(pair, 120)
                if momentum and momentum.get("volatility", 0) > 0:
                    atr = momentum["last_price"] * momentum["volatility"] / 100 * 10
                else:
                    continue

            # Distancia normalizada por ATR
            distance_atr = abs_diff / atr if atr > 0 else 0

            # Filtro mínimo de distancia
            if distance_atr < config.CRYPTO_ARB_MIN_DISTANCE_ATR:
                continue

            # Consistencia de tendencia (últimos 60-120 seg)
            trend_window = min(int(time_remaining * 0.8), 120)
            trend = self.feed.get_trend_consistency(pair, max(trend_window, 30))
            trend_consistency = 0.5
            trend_aligned = False
            if trend:
                trend_consistency = trend["consistency"]
                trend_aligned = trend["direction"] == direction

            # La tendencia debe estar alineada con la dirección
            if not trend_aligned and trend_consistency > 0.6:
                continue  # Tendencia fuerte en contra, no apostar

            # Filtro mínimo de consistencia
            if trend_aligned and trend_consistency < config.CRYPTO_ARB_MIN_TREND_CONSISTENCY:
                continue

            # ── Calcular score compuesto ──
            total_time = float(interval)
            time_factor = 1.0 - (time_remaining / total_time)
            time_factor = max(0.0, min(time_factor, 1.0))

            # Factor consistencia: 0.5-1.0 → mapeado a 0.0-1.0
            consistency_factor = (trend_consistency - 0.5) * 2.0 if trend_aligned else 0.0
            consistency_factor = max(0.0, min(consistency_factor, 1.0))

            # Score = distancia_normalizada × factor_tiempo × factor_consistencia
            score = distance_atr * time_factor * (0.5 + 0.5 * consistency_factor)

            # v8.0: Multi-timeframe boost — verificar múltiples ventanas temporales
            mtf_boost = 0.0
            if config.FEATURE_MULTI_TIMEFRAME:
                agreements = 0
                for window in config.MTF_WINDOWS:
                    mtf_mom = self.feed.get_momentum(pair, window)
                    if mtf_mom and mtf_mom["direction"] == direction:
                        agreements += 1
                if agreements >= config.MTF_MIN_AGREEMENT:
                    mtf_boost = config.MTF_BOOST_POINTS / 100.0  # Boost como fracción
                    score *= (1.0 + mtf_boost)

            # v8.0: VWAP — verificar desviación del precio vs VWAP
            vwap_aligned = False
            if config.FEATURE_VWAP:
                vwap_data = self.feed.get_vwap(pair, config.VWAP_LOOKBACK_SEC) if hasattr(self.feed, 'get_vwap') else None
                if vwap_data:
                    vwap_price = vwap_data.get("vwap", 0)
                    if vwap_price > 0:
                        deviation_pct = abs(current_price - vwap_price) / vwap_price * 100
                        if deviation_pct >= config.VWAP_MIN_DEVIATION_PCT:
                            # Precio por encima de VWAP + direction up = confirmación
                            if (direction == "up" and current_price > vwap_price) or \
                               (direction == "down" and current_price < vwap_price):
                                vwap_aligned = True
                                score *= 1.1  # 10% boost

            if score < config.CRYPTO_ARB_MIN_SCORE:
                continue

            # v9.0: RSI — confirmar dirección con RSI
            rsi_aligned = False
            if config.FEATURE_RSI:
                rsi_data = self.feed.get_rsi(pair, config.RSI_PERIOD, config.RSI_CANDLE_SEC)
                if rsi_data:
                    rsi_val = rsi_data["rsi"]
                    # RSI > 70 + direction up = momentum alcista confirmado
                    # RSI < 30 + direction down = momentum bajista confirmado
                    if (direction == "up" and rsi_val > config.RSI_OVERBOUGHT) or \
                       (direction == "down" and rsi_val < config.RSI_OVERSOLD):
                        rsi_aligned = True
                        score *= (1.0 + config.RSI_BOOST_PCT / 100.0)
                    # RSI contradice dirección → penalizar
                    elif (direction == "up" and rsi_val < config.RSI_OVERSOLD) or \
                         (direction == "down" and rsi_val > config.RSI_OVERBOUGHT):
                        score *= 0.85  # -15% penalización
                    if "score_details" not in dir():
                        pass  # score_details se crea después
                    # Se agrega al score_details más abajo

            # v9.0: MACD — confirmar tendencia con cruce MACD
            macd_aligned = False
            if config.FEATURE_MACD:
                macd_data = self.feed.get_macd(
                    pair, config.MACD_FAST, config.MACD_SLOW,
                    config.MACD_SIGNAL, config.MACD_CANDLE_SEC
                )
                if macd_data:
                    # MACD bullish + direction up = confirmación
                    if (direction == "up" and macd_data["bullish"]) or \
                       (direction == "down" and macd_data["bearish"]):
                        macd_aligned = True
                        score *= (1.0 + config.MACD_BOOST_PCT / 100.0)
                    # Cruce reciente da boost extra
                    if (direction == "up" and macd_data["bullish_cross"]) or \
                       (direction == "down" and macd_data["bearish_cross"]):
                        score *= 1.05  # +5% extra por cruce fresco

            # v8.0: OrderBook Crypto — verificar liquidez antes de señal
            if config.FEATURE_ORDERBOOK_CRYPTO:
                tokens = mdata.get("tokens", [])
                # Usar el spread implícito como proxy de liquidez
                if tokens and len(tokens) >= 2:
                    prices = [float(t.get("price", 0.5)) for t in tokens]
                    spread = abs(prices[0] + prices[1] - 1.0)  # Debería ser ~0 en mercado líquido
                    if spread > config.ORDERBOOK_CRYPTO_MAX_IMPACT_PCT / 100:
                        continue  # Mercado muy ilíquido, skip

            # Odds de Polymarket
            tokens = mdata.get("tokens", [])
            up_odds = down_odds = None
            for tok in tokens:
                outcome = tok.get("outcome", "").lower()
                price = float(tok.get("price", 0.5))
                if outcome == "up":
                    up_odds = price
                elif outcome == "down":
                    down_odds = price

            if up_odds is None or down_odds is None:
                continue

            poly_odds = up_odds if direction == "up" else down_odds

            # Estimar probabilidad real basada en el score
            # Score 0.4 → ~65%, Score 0.7 → ~80%, Score 1.0+ → ~90%
            fair_odds = min(0.55 + score * 0.35, 0.95)

            edge_pct = (fair_odds - poly_odds) * 100
            confidence = min(score * 100, 100)

            # Detalles del score para el dashboard
            score_details = {
                "price_to_beat": round(price_to_beat, 2),
                "current_price": round(current_price, 2),
                "price_diff": round(price_diff, 2),
                "distance_atr": round(distance_atr, 3),
                "time_factor": round(time_factor, 3),
                "trend_consistency": round(trend_consistency, 3),
                "trend_aligned": trend_aligned,
                "consistency_factor": round(consistency_factor, 3),
                "atr": round(atr, 2),
                "score": round(score, 3),
                "rsi_aligned": rsi_aligned,
                "macd_aligned": macd_aligned,
                "vwap_aligned": vwap_aligned,
            }

            signal = CryptoSignal(
                coin=coin,
                direction=direction,
                spot_change_pct=round((price_diff / price_to_beat) * 100, 4) if price_to_beat else 0,
                poly_odds=poly_odds,
                fair_odds=fair_odds,
                confidence=confidence,
                edge_pct=edge_pct,
                condition_id=cid,
                market_question=mdata["question"],
                spot_price=current_price,
                time_remaining_sec=int(time_remaining),
                end_date=mdata.get("end_date"),
                event_slug=mdata.get("event_slug", ""),
                strategy="score",
                score_details=score_details,
            )
            signals.append(signal)

        return signals

    def _estimate_fair_odds(self, change_pct: float, momentum: dict,
                            time_remaining: float) -> float:
        """Estimar probabilidad real basada en el momentum del spot."""
        # Base: si se movió X%, la probabilidad de continuar es alta
        # pero depende del tiempo restante
        base = 0.5

        # Factor momentum: más movimiento = más probabilidad
        # Umbrales adaptativos según tiempo restante
        if change_pct >= 0.5:
            base = 0.92
        elif change_pct >= 0.3:
            base = 0.85
        elif change_pct >= 0.2:
            base = 0.78
        elif change_pct >= 0.12:
            base = 0.72
        elif change_pct >= 0.06:
            base = 0.66
        elif change_pct >= 0.04:
            base = 0.62
        else:
            base = 0.58

        # Factor tiempo: más tiempo restante = más incertidumbre
        if time_remaining > 600:  # >10 min
            base *= 0.90
        elif time_remaining > 300:  # >5 min
            base *= 0.95
        # <5 min: movimiento ya está consolidado

        # Factor velocidad: momentum acelerando = más confiable
        speed = abs(momentum.get("speed_per_sec", 0))
        if speed > 0.001:  # Rápido
            base = min(base * 1.05, 0.98)

        return round(min(base, 0.98), 3)

    def _calc_confidence(self, change_pct: float, momentum: dict,
                         poly_odds: float, fair_odds: float,
                         time_remaining: float) -> float:
        """Calcular confianza 0-100 de la señal."""
        score = 0

        # Magnitud del movimiento (0-30)
        if change_pct >= 0.5:
            score += 30
        elif change_pct >= 0.3:
            score += 25
        elif change_pct >= 0.2:
            score += 20
        elif change_pct >= 0.12:
            score += 18
        elif change_pct >= 0.06:
            score += 15
        elif change_pct >= 0.04:
            score += 12

        # Edge vs odds actuales (0-30)
        edge = fair_odds - poly_odds
        if edge >= 0.30:
            score += 30
        elif edge >= 0.20:
            score += 25
        elif edge >= 0.15:
            score += 20
        elif edge >= 0.10:
            score += 18
        elif edge >= 0.05:
            score += 14

        # Consistencia del momentum (0-20)
        ticks = momentum.get("ticks", 0)
        if ticks >= 50:
            score += 20
        elif ticks >= 20:
            score += 15
        elif ticks >= 10:
            score += 12
        elif ticks >= 5:
            score += 8

        # Timing (0-20) — sweet spot es 2-8 min antes del cierre
        if 120 <= time_remaining <= 480:
            score += 20
        elif 60 <= time_remaining <= 720:
            score += 15
        else:
            score += 5

        return min(score, 100)

    def _record_signal(self, signal: CryptoSignal):
        """Registrar señal y verificar límite diario."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._signals_today_date != today:
            self._signals_today = []
            self._signals_today_date = today

        # Evitar señales duplicadas para el mismo mercado (1 señal por condition_id)
        for s in self._signals_today:
            if s.condition_id == signal.condition_id:
                return

        if len(self._signals_today) >= config.CRYPTO_ARB_MAX_DAILY:
            return

        self._signals_today.append(signal)

    def get_recent_signals(self, limit: int = 50) -> list[dict]:
        """Señales recientes para el dashboard. Recalcula tiempo restante en vivo."""
        now = datetime.now(timezone.utc)
        # Solo mostrar señales de los últimos 30 min (resueltas se muestran 5 min extra)
        cutoff = now - timedelta(minutes=30)
        result = []
        for s in reversed(self._signals_today[-limit * 2:]):
            if s.timestamp < cutoff and s.resolved:
                continue  # Señal vieja y resuelta, no mostrar
            # Recalcular tiempo restante en vivo
            if s.end_date:
                remaining = max(0, int((s.end_date - now).total_seconds()))
            else:
                remaining = max(0, s.time_remaining_sec - int((now - s.timestamp).total_seconds()))
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
                "polymarket_url": f"https://polymarket.com/event/{s.event_slug}" if s.event_slug else "",
                "strategy": s.strategy,
                "score_details": s.score_details,
                "resolved": s.resolved,
                "result": s.result,
                "paper_pnl": s.paper_pnl,
            })
            if len(result) >= limit:
                break
        return result

    def resolve_signal(self, condition_id: str, result: str, pnl: float):
        """Marcar una señal en memoria como resuelta."""
        for s in self._signals_today:
            if s.condition_id == condition_id and not s.resolved:
                s.resolved = True
                s.result = result
                s.paper_pnl = pnl

    def get_active_markets(self) -> list[dict]:
        """Mercados activos para el dashboard."""
        now = datetime.now(timezone.utc)
        result = []
        for cid, m in self._active_markets.items():
            end_date = m.get("end_date")
            try:
                remaining = (end_date - now).total_seconds() if end_date else 0
            except TypeError:
                remaining = 0
            pair = PAIR_MAP.get(m["coin"], "")
            momentum = self.feed.get_momentum(pair, config.CRYPTO_ARB_LOOKBACK_SEC)

            tokens = m.get("tokens", [])
            up_odds = down_odds = None
            for tok in tokens:
                if tok.get("outcome", "").lower() == "up":
                    up_odds = float(tok.get("price", 0.5))
                elif tok.get("outcome", "").lower() == "down":
                    down_odds = float(tok.get("price", 0.5))

            result.append({
                "condition_id": cid,
                "question": m["question"],
                "coin": m["coin"],
                "time_remaining_sec": int(max(remaining, 0)),
                "up_odds": up_odds,
                "down_odds": down_odds,
                "spot_price": self.feed.get_price(pair),
                "momentum": momentum,
            })
        return sorted(result, key=lambda x: x["time_remaining_sec"])

    async def check_price_sum_arb(self) -> list[dict]:
        """Detectar oportunidades de Price-Sum Arbitrage: YES + NO != $1.
        Si la suma < $1, podemos comprar ambos y ganar la diferencia.
        Si la suma > $1, hay una oportunidad de venta.
        """
        opportunities = []
        for cid, mdata in self._active_markets.items():
            tokens = mdata.get("tokens", [])
            if len(tokens) < 2:
                continue

            up_price = down_price = None
            up_token = down_token = None
            for tok in tokens:
                outcome = tok.get("outcome", "").lower()
                price = float(tok.get("price", 0))
                if outcome in ("up", "yes"):
                    up_price = price
                    up_token = tok.get("token_id", "")
                elif outcome in ("down", "no"):
                    down_price = price
                    down_token = tok.get("token_id", "")

            if up_price is None or down_price is None:
                continue
            if up_price <= 0 or down_price <= 0:
                continue

            price_sum = up_price + down_price
            gap_pct = abs(1.0 - price_sum) * 100

            # Solo reportar si el gap es significativo (> 2%)
            if gap_pct < 2.0:
                continue

            opp_type = "buy_both" if price_sum < 1.0 else "overpriced"
            profit_pct = (1.0 - price_sum) * 100 if opp_type == "buy_both" else (price_sum - 1.0) * 100

            opportunities.append({
                "condition_id": cid,
                "coin": mdata["coin"],
                "question": mdata["question"],
                "up_price": up_price,
                "down_price": down_price,
                "price_sum": round(price_sum, 4),
                "gap_pct": round(gap_pct, 2),
                "profit_pct": round(profit_pct, 2),
                "type": opp_type,
                "up_token": up_token,
                "down_token": down_token,
            })

        return sorted(opportunities, key=lambda x: -x["gap_pct"])

    def get_stats(self) -> dict:
        """Estadísticas para el dashboard."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        signals_today = [s for s in self._signals_today
                         if s.timestamp.strftime("%Y-%m-%d") == today]
        return {
            "active_markets": len(self._active_markets),
            "signals_today": len(signals_today),
            "max_daily": config.CRYPTO_ARB_MAX_DAILY,
            "mode": config.CRYPTO_ARB_MODE,
            "avg_confidence": (
                round(sum(s.confidence for s in signals_today) / len(signals_today), 1)
                if signals_today else 0
            ),
            "avg_edge": (
                round(sum(s.edge_pct for s in signals_today) / len(signals_today), 1)
                if signals_today else 0
            ),
        }
