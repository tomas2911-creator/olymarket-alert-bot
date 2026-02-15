"""Spike Detection Bot — Detectar movimientos bruscos y tradear reversión/momentum."""
import time
from collections import defaultdict
from src import config


class SpikeDetector:
    """Detecta movimientos bruscos de precio en mercados Polymarket."""

    def __init__(self, db):
        self.db = db
        self._price_history = defaultdict(list)  # market_id -> [(timestamp, price)]
        self._daily_trades = 0
        self._daily_reset = 0

    async def tick(self, markets: list):
        """Procesar mercados buscando spikes."""
        if not config.FEATURE_SPIKE_DETECTION:
            return []

        now = time.time()
        # Reset diario
        if now - self._daily_reset > 86400:
            self._daily_trades = 0
            self._daily_reset = now

        if self._daily_trades >= config.SPIKE_MAX_DAILY:
            return []

        signals = []
        for m in (markets or []):
            mid = m.get("condition_id") or m.get("market_id", "")
            price = m.get("price", 0)
            if not mid or price <= 0:
                continue

            # Registrar precio
            self._price_history[mid].append((now, price))
            # Limpiar precios viejos (> 30 min)
            self._price_history[mid] = [
                (t, p) for t, p in self._price_history[mid]
                if now - t < 1800
            ]

            # Buscar spike
            signal = self._detect_spike(mid, now)
            if signal:
                signals.append(signal)

        return signals

    def _detect_spike(self, market_id: str, now: float) -> dict:
        """Detectar si hay un spike en el mercado."""
        history = self._price_history.get(market_id, [])
        lookback = config.SPIKE_LOOKBACK_MIN * 60

        recent = [(t, p) for t, p in history if now - t <= lookback]
        if len(recent) < 3:
            return None

        oldest_price = recent[0][1]
        current_price = recent[-1][1]

        if oldest_price == 0:
            return None

        change_pct = abs((current_price - oldest_price) / oldest_price) * 100

        if change_pct < config.SPIKE_MIN_MOVE_PCT:
            return None

        direction = "up" if current_price > oldest_price else "down"

        # Determinar acción según estrategia
        if config.SPIKE_STRATEGY == "mean_reversion":
            # Contra el spike
            action = "sell" if direction == "up" else "buy"
        else:  # momentum
            # Con el spike
            action = "buy" if direction == "up" else "sell"

        self._daily_trades += 1

        return {
            "market_id": market_id,
            "spike_pct": round(change_pct, 2),
            "direction": direction,
            "action": action,
            "strategy": config.SPIKE_STRATEGY,
            "price": current_price,
            "bet_size": config.SPIKE_BET_SIZE,
            "timestamp": now,
        }

    def get_stats(self) -> dict:
        return {
            "enabled": config.FEATURE_SPIKE_DETECTION,
            "markets_tracked": len(self._price_history),
            "daily_trades": self._daily_trades,
            "strategy": config.SPIKE_STRATEGY,
        }
