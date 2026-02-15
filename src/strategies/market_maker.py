"""Market Making Bot — Poner órdenes a ambos lados del spread."""
import asyncio
import time
from src import config


class MarketMakerBot:
    """Bot de market making para mercados de alta liquidez en Polymarket."""

    def __init__(self, db, clob_client=None):
        self.db = db
        self.clob = clob_client
        self._running = False
        self._positions = {}  # market_id -> {inventory, avg_price, orders}
        self._daily_pnl = 0.0
        self._last_refresh = 0

    async def start(self):
        """Iniciar el bot de market making."""
        if not config.FEATURE_MARKET_MAKING:
            return
        self._running = True
        print("Market Making Bot iniciado", flush=True)

    async def stop(self):
        """Detener y cancelar órdenes abiertas."""
        self._running = False
        # Cancelar todas las órdenes abiertas
        for mid, pos in self._positions.items():
            for oid in pos.get("orders", []):
                try:
                    if self.clob:
                        await self.clob.cancel_order(oid)
                except Exception as e:
                    print(f"Error cancelando orden {oid}: {e}", flush=True)
        self._positions.clear()
        print("Market Making Bot detenido", flush=True)

    async def tick(self):
        """Ejecutar un ciclo del market maker."""
        if not self._running or not config.FEATURE_MARKET_MAKING:
            return

        now = time.time()
        if now - self._last_refresh < config.MM_REFRESH_SEC:
            return
        self._last_refresh = now

        try:
            # Obtener mercados elegibles (alta liquidez)
            markets = await self._get_eligible_markets()
            for market in markets[:5]:  # Máximo 5 mercados simultáneos
                await self._manage_market(market)
        except Exception as e:
            print(f"Error en market maker tick: {e}", flush=True)

    async def _get_eligible_markets(self) -> list:
        """Buscar mercados con suficiente liquidez para market making."""
        try:
            markets = await self.db.get_active_markets()
            eligible = []
            for m in (markets or []):
                liquidity = m.get("volume", 0)
                if liquidity >= config.MM_MIN_LIQUIDITY:
                    eligible.append(m)
            return sorted(eligible, key=lambda x: x.get("volume", 0), reverse=True)
        except Exception:
            return []

    async def _manage_market(self, market: dict):
        """Gestionar órdenes en un mercado específico."""
        mid = market.get("condition_id") or market.get("market_id", "")
        if not mid:
            return

        # Verificar inventario máximo
        pos = self._positions.get(mid, {"inventory": 0, "avg_price": 0, "orders": []})
        if abs(pos["inventory"]) >= config.MM_MAX_INVENTORY:
            return

        # Calcular spread
        spread = config.MM_SPREAD_PCT / 100.0
        mid_price = market.get("price", 0.5)
        if mid_price <= 0.05 or mid_price >= 0.95:
            return  # No hacer MM en precios extremos

        bid_price = round(mid_price - spread / 2, 4)
        ask_price = round(mid_price + spread / 2, 4)
        size = config.MM_ORDER_SIZE

        # Registrar intención (paper trading por defecto)
        await self.db.log_mm_order(mid, "BID", bid_price, size)
        await self.db.log_mm_order(mid, "ASK", ask_price, size)

        self._positions[mid] = pos

    def get_stats(self) -> dict:
        """Retornar estadísticas del market maker."""
        return {
            "running": self._running,
            "active_markets": len(self._positions),
            "daily_pnl": self._daily_pnl,
            "positions": {k: {"inventory": v["inventory"]} for k, v in self._positions.items()},
        }
