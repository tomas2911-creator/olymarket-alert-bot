"""WebSocket client para Polymarket — Recibir trades en tiempo real.

Usa el WebSocket del CLOB para recibir actualizaciones de mercados
y trades en tiempo real. Complementa el polling para capturar
trades que ocurren entre ciclos.
"""
import asyncio
import json
import time
from src import config


class PolymarketWebSocket:
    """Cliente WebSocket para recibir trades de Polymarket en tiempo real."""

    def __init__(self, on_trade=None):
        self._on_trade = on_trade  # callback async para procesar trades
        self._running = False
        self._connected = False
        self._ws = None
        self._last_message = 0
        self._messages_received = 0
        self._trades_received = 0
        self._reconnects = 0
        self._subscribed_markets = set()
        self._pending_subs = set()  # mercados pendientes de suscribir

    async def start(self):
        """Iniciar conexión WebSocket."""
        if not config.FEATURE_WEBSOCKET:
            return
        self._running = True
        asyncio.create_task(self._connect_loop())
        print("[WS] Polymarket WebSocket: iniciando conexión", flush=True)

    async def stop(self):
        """Detener WebSocket."""
        self._running = False
        self._connected = False

    async def subscribe(self, market_ids: list):
        """Suscribirse a mercados. Si ya conectado, envía suscripción inmediata."""
        new_ids = set(market_ids) - self._subscribed_markets
        if not new_ids:
            return
        self._subscribed_markets.update(new_ids)
        if self._connected and self._ws:
            for mid in new_ids:
                try:
                    # Suscribir a eventos de trade del mercado
                    sub_msg = json.dumps({
                        "type": "subscribe",
                        "channel": "market",
                        "assets_id": mid,
                    })
                    await self._ws.send(sub_msg)
                except Exception:
                    self._pending_subs.add(mid)
        else:
            self._pending_subs.update(new_ids)

    async def _connect_loop(self):
        """Loop de conexión con reconexión automática y backoff."""
        backoff = config.WS_RECONNECT_DELAY
        while self._running:
            try:
                await self._connect()
                backoff = config.WS_RECONNECT_DELAY  # reset backoff on success
            except Exception as e:
                self._connected = False
                self._reconnects += 1
                # Solo loguear cada 5 reconexiones para no spamear
                if self._reconnects % 5 == 1:
                    print(f"[WS] Error (reconexión #{self._reconnects}): {e}", flush=True)
                await asyncio.sleep(min(backoff, 60))
                backoff = min(backoff * 1.5, 60)  # backoff exponencial, máx 60s

    async def _connect(self):
        """Establecer conexión WebSocket al CLOB."""
        try:
            import websockets
        except ImportError:
            print("[WS] Módulo 'websockets' no instalado", flush=True)
            self._running = False
            return

        url = config.WS_POLYMARKET_URL
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._connected = True
            print(f"[WS] Conectado a {url} | {len(self._subscribed_markets)} mercados", flush=True)

            # Enviar suscripciones pendientes + existentes
            all_subs = self._subscribed_markets | self._pending_subs
            self._pending_subs.clear()
            for mid in all_subs:
                try:
                    sub_msg = json.dumps({
                        "type": "subscribe",
                        "channel": "market",
                        "assets_id": mid,
                    })
                    await ws.send(sub_msg)
                except Exception:
                    pass

            # Loop de recepción
            async for message in ws:
                if not self._running:
                    break
                self._last_message = time.time()
                self._messages_received += 1

                try:
                    data = json.loads(message)
                    msg_type = data.get("type", data.get("event_type", ""))

                    # Procesar trades (diferentes formatos posibles del CLOB WS)
                    if self._on_trade:
                        if msg_type in ("trade", "last_trade_price", "tick"):
                            await self._on_trade(data)
                        elif "price" in data and "size" in data:
                            # Mensaje con datos de trade sin type explícito
                            data["type"] = "trade"
                            await self._on_trade(data)
                        elif msg_type == "book" and "trades" in data:
                            # Batch de trades
                            for trade in data["trades"]:
                                self._trades_received += 1
                                await self._on_trade(trade)
                                continue

                    if msg_type in ("trade", "last_trade_price", "tick"):
                        self._trades_received += 1

                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    if self._messages_received % 100 == 0:
                        print(f"[WS] Error procesando mensaje #{self._messages_received}: {e}", flush=True)

        self._ws = None
        self._connected = False

    def get_stats(self) -> dict:
        return {
            "enabled": config.FEATURE_WEBSOCKET,
            "connected": self._connected,
            "messages_received": self._messages_received,
            "trades_received": self._trades_received,
            "reconnects": self._reconnects,
            "subscribed_markets": len(self._subscribed_markets),
            "last_message_ago": round(time.time() - self._last_message, 1) if self._last_message else None,
        }
