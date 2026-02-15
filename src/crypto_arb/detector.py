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
}

PAIR_MAP = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
}


class CryptoArbDetector:
    """Detecta oportunidades de arbitraje en mercados crypto up/down de Polymarket."""

    def __init__(self, binance_feed: BinanceFeed):
        self.feed = binance_feed
        self._active_markets: dict[str, dict] = {}  # condition_id -> market_data
        self._last_scan = 0.0
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

                # Buscar divergencias cada 3 segundos
                signals = await self._check_divergences()
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
                if "ETH" in enabled_coins:
                    slug_templates.append(("ETH", "eth-updown-15m", 900))
                if "SOL" in enabled_coins:
                    slug_templates.append(("SOL", "sol-updown-15m", 900))

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
                                    "tokens": [],
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
