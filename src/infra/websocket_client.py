"""WebSocket client para Polymarket — Recibir trades en tiempo real."""
import asyncio
import json
import time
from src import config


class PolymarketWebSocket:
    """Cliente WebSocket para recibir actualizaciones de precio en tiempo real."""

    def __init__(self, on_trade=None):
        self._on_trade = on_trade  # callback async para procesar trades
        self._running = False
        self._connected = False
        self._last_message = 0
        self._messages_received = 0
        self._reconnects = 0
        self._subscribed_markets = set()

    async def start(self):
        """Iniciar conexión WebSocket."""
        if not config.FEATURE_WEBSOCKET:
            return
        self._running = True
        asyncio.create_task(self._connect_loop())
        print("WebSocket Polymarket: iniciando conexión", flush=True)

    async def stop(self):
        """Detener WebSocket."""
        self._running = False
        self._connected = False

    async def subscribe(self, market_ids: list):
        """Suscribirse a mercados específicos."""
        self._subscribed_markets.update(market_ids)

    async def _connect_loop(self):
        """Loop de conexión con reconexión automática."""
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                print(f"WebSocket error: {e}", flush=True)
                self._connected = False
                self._reconnects += 1
                await asyncio.sleep(config.WS_RECONNECT_DELAY)

    async def _connect(self):
        """Establecer conexión WebSocket."""
        try:
            import websockets
        except ImportError:
            print("WebSocket: módulo 'websockets' no instalado. Instalar con: pip install websockets", flush=True)
            self._running = False
            return

        url = config.WS_POLYMARKET_URL
        async with websockets.connect(url) as ws:
            self._connected = True
            print(f"WebSocket conectado a {url}", flush=True)

            # Suscribirse a mercados
            if self._subscribed_markets:
                for mid in self._subscribed_markets:
                    sub_msg = json.dumps({"type": "subscribe", "market": mid})
                    await ws.send(sub_msg)

            # Loop de recepción
            async for message in ws:
                if not self._running:
                    break
                self._last_message = time.time()
                self._messages_received += 1

                try:
                    data = json.loads(message)
                    if self._on_trade and data.get("type") == "trade":
                        await self._on_trade(data)
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    print(f"WebSocket process error: {e}", flush=True)

    def get_stats(self) -> dict:
        return {
            "enabled": config.FEATURE_WEBSOCKET,
            "connected": self._connected,
            "messages_received": self._messages_received,
            "reconnects": self._reconnects,
            "subscribed_markets": len(self._subscribed_markets),
            "last_message_ago": round(time.time() - self._last_message, 1) if self._last_message else None,
        }
