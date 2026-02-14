"""Feed de precios en tiempo real desde Binance WebSocket."""
import asyncio
import json
import time
from collections import deque
from typing import Optional
import structlog

logger = structlog.get_logger()

# Binance WebSocket público (sin API key)
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"


class PriceTick:
    """Un tick de precio con timestamp."""
    __slots__ = ("price", "ts")

    def __init__(self, price: float, ts: float):
        self.price = price
        self.ts = ts


class BinanceFeed:
    """Mantiene precios en tiempo real de múltiples pares via WebSocket de Binance.

    Usa httpx polling como fallback si WebSocket falla (Railway puede tener
    restricciones de WebSocket saliente).
    """

    def __init__(self, pairs: list[str], max_history: int = 600):
        self.pairs = [p.lower() for p in pairs]
        self._history: dict[str, deque[PriceTick]] = {
            p: deque(maxlen=max_history) for p in self.pairs
        }
        self._latest: dict[str, float] = {}
        self._running = False
        self._ws_connected = False

    @property
    def is_running(self) -> bool:
        return self._running

    def get_price(self, pair: str) -> Optional[float]:
        """Precio más reciente de un par."""
        return self._latest.get(pair.lower())

    def get_history(self, pair: str, seconds: int = 180) -> list[PriceTick]:
        """Ticks de los últimos N segundos."""
        pair = pair.lower()
        if pair not in self._history:
            return []
        cutoff = time.time() - seconds
        return [t for t in self._history[pair] if t.ts >= cutoff]

    def get_momentum(self, pair: str, seconds: int = 180) -> Optional[dict]:
        """Calcular momentum: cambio % y dirección en ventana de tiempo."""
        ticks = self.get_history(pair, seconds)
        if len(ticks) < 2:
            return None
        first_price = ticks[0].price
        last_price = ticks[-1].price
        if first_price <= 0:
            return None
        change_pct = ((last_price - first_price) / first_price) * 100
        # Calcular velocidad: pendiente por segundo
        elapsed = ticks[-1].ts - ticks[0].ts
        speed = change_pct / elapsed if elapsed > 0 else 0
        # Volumen de cambios (volatilidad)
        changes = []
        for i in range(1, len(ticks)):
            c = abs(ticks[i].price - ticks[i - 1].price) / ticks[i - 1].price * 100
            changes.append(c)
        volatility = sum(changes) / len(changes) if changes else 0
        return {
            "pair": pair,
            "first_price": first_price,
            "last_price": last_price,
            "change_pct": round(change_pct, 4),
            "direction": "up" if change_pct > 0 else "down",
            "speed_per_sec": round(speed, 6),
            "volatility": round(volatility, 6),
            "ticks": len(ticks),
            "window_sec": round(elapsed, 1),
        }

    async def start(self):
        """Iniciar feed. Intenta WebSocket primero, fallback a polling HTTP."""
        self._running = True
        logger.info("binance_feed_starting", pairs=self.pairs)
        # Intentar WebSocket, si falla usar polling
        try:
            await self._run_websocket()
        except Exception as e:
            logger.warning("binance_ws_failed_using_polling", error=str(e))
            await self._run_polling()

    async def stop(self):
        self._running = False

    async def _run_websocket(self):
        """Conectar a Binance WebSocket y recibir trades en tiempo real."""
        import websockets
        streams = "/".join(f"{p}@trade" for p in self.pairs)
        url = f"{BINANCE_WS_URL}/{streams}"
        logger.info("binance_ws_connecting", url=url[:80])

        async for ws in websockets.connect(url, ping_interval=20, ping_timeout=10):
            try:
                self._ws_connected = True
                logger.info("binance_ws_connected")
                async for msg in ws:
                    if not self._running:
                        break
                    try:
                        data = json.loads(msg)
                        pair = data.get("s", "").lower()
                        price = float(data.get("p", 0))
                        ts = data.get("T", 0) / 1000.0 if data.get("T") else time.time()
                        if pair and price > 0:
                            # Binance devuelve "BTCUSDT" pero nuestros pares son "btcusdt"
                            if pair in self._history:
                                self._history[pair].append(PriceTick(price, ts))
                                self._latest[pair] = price
                    except (ValueError, KeyError):
                        pass
            except websockets.ConnectionClosed:
                self._ws_connected = False
                if self._running:
                    logger.warning("binance_ws_disconnected_reconnecting")
                    await asyncio.sleep(2)
                    continue
                break
            except Exception as e:
                self._ws_connected = False
                logger.error("binance_ws_error", error=str(e))
                if self._running:
                    await asyncio.sleep(5)
                    continue
                break

    async def _run_polling(self):
        """Fallback: polling HTTP cada 2 segundos para precio."""
        import httpx
        logger.info("binance_polling_started")
        async with httpx.AsyncClient(timeout=10) as client:
            while self._running:
                try:
                    for pair in self.pairs:
                        resp = await client.get(
                            f"https://api.binance.com/api/v3/ticker/price",
                            params={"symbol": pair.upper()},
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            price = float(data.get("price", 0))
                            if price > 0:
                                now = time.time()
                                self._history[pair].append(PriceTick(price, now))
                                self._latest[pair] = price
                except Exception as e:
                    logger.warning("binance_polling_error", error=str(e))
                await asyncio.sleep(2)

    def get_status(self) -> dict:
        """Estado actual del feed para el dashboard."""
        return {
            "running": self._running,
            "ws_connected": self._ws_connected,
            "pairs": {
                p: {
                    "price": self._latest.get(p),
                    "ticks": len(self._history.get(p, [])),
                }
                for p in self.pairs
            },
        }
