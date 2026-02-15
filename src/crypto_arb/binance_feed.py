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
        # Historia muestreada: 1 tick/segundo, 30 min de datos
        self._sampled: dict[str, deque[PriceTick]] = {
            p: deque(maxlen=1800) for p in self.pairs
        }
        self._last_sample_ts: dict[str, float] = {p: 0.0 for p in self.pairs}
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

    def get_sampled_history(self, pair: str, seconds: int = 900) -> list[PriceTick]:
        """Ticks muestreados (1/seg) de los últimos N segundos. Más historia que get_history."""
        pair = pair.lower()
        if pair not in self._sampled:
            return []
        cutoff = time.time() - seconds
        return [t for t in self._sampled[pair] if t.ts >= cutoff]

    def get_price_at_time(self, pair: str, target_ts: float, tolerance_sec: float = 5.0) -> Optional[float]:
        """Buscar el precio más cercano a un timestamp dado."""
        pair = pair.lower()
        ticks = list(self._sampled.get(pair, []))
        if not ticks:
            return None
        best = None
        best_diff = float('inf')
        for t in ticks:
            diff = abs(t.ts - target_ts)
            if diff < best_diff:
                best_diff = diff
                best = t.price
        return best if best_diff <= tolerance_sec else None

    def get_atr(self, pair: str, window_sec: int = 300, candle_sec: int = 10) -> Optional[float]:
        """Calcular ATR (Average True Range) en ventana de tiempo.
        Agrupa ticks en velas de candle_sec segundos y calcula el rango promedio.
        """
        ticks = self.get_sampled_history(pair, window_sec)
        if len(ticks) < 10:
            return None
        # Agrupar en velas
        candles = []
        bucket_start = ticks[0].ts
        bucket_high = bucket_low = ticks[0].price
        prev_close = ticks[0].price
        for t in ticks[1:]:
            if t.ts - bucket_start >= candle_sec:
                # True Range = max(high-low, |high-prev_close|, |low-prev_close|)
                tr = max(
                    bucket_high - bucket_low,
                    abs(bucket_high - prev_close),
                    abs(bucket_low - prev_close)
                )
                candles.append(tr)
                prev_close = t.price
                bucket_start = t.ts
                bucket_high = bucket_low = t.price
            else:
                bucket_high = max(bucket_high, t.price)
                bucket_low = min(bucket_low, t.price)
        if not candles:
            return None
        return sum(candles) / len(candles)

    def get_trend_consistency(self, pair: str, seconds: int = 120) -> Optional[dict]:
        """Calcular qué tan consistente es la tendencia reciente.
        Retorna % de ticks que van en la dirección dominante.
        """
        ticks = self.get_sampled_history(pair, seconds)
        if len(ticks) < 5:
            return None
        up_moves = 0
        down_moves = 0
        for i in range(1, len(ticks)):
            if ticks[i].price > ticks[i-1].price:
                up_moves += 1
            elif ticks[i].price < ticks[i-1].price:
                down_moves += 1
        total_moves = up_moves + down_moves
        if total_moves == 0:
            return {"direction": "flat", "consistency": 0.5, "up_ratio": 0.5}
        dominant = "up" if up_moves >= down_moves else "down"
        consistency = max(up_moves, down_moves) / total_moves
        return {
            "direction": dominant,
            "consistency": round(consistency, 3),
            "up_ratio": round(up_moves / total_moves, 3),
            "up_moves": up_moves,
            "down_moves": down_moves,
        }

    def get_volume_intensity(self, pair: str, seconds: int = 60) -> Optional[float]:
        """Intensidad de volumen: trades por segundo en la ventana reciente."""
        pair = pair.lower()
        if pair not in self._history:
            return None
        cutoff = time.time() - seconds
        recent = [t for t in self._history[pair] if t.ts >= cutoff]
        if len(recent) < 2:
            return None
        elapsed = recent[-1].ts - recent[0].ts
        return len(recent) / elapsed if elapsed > 0 else 0

    def get_vwap(self, pair: str, seconds: int = 600) -> Optional[dict]:
        """Calcular VWAP (Volume Weighted Average Price) aproximado.
        Usa intensidad de ticks como proxy de volumen (sin volumen real de WS).
        """
        ticks = self.get_sampled_history(pair, seconds)
        if len(ticks) < 10:
            return None
        # VWAP simplificado: ponderar precios por "actividad" (cambio absoluto)
        total_weight = 0.0
        weighted_price = 0.0
        for i in range(1, len(ticks)):
            weight = abs(ticks[i].price - ticks[i-1].price) + 0.001  # evitar 0
            weighted_price += ticks[i].price * weight
            total_weight += weight
        if total_weight <= 0:
            return None
        vwap = weighted_price / total_weight
        current = ticks[-1].price
        deviation_pct = abs(current - vwap) / vwap * 100 if vwap > 0 else 0
        return {
            "vwap": round(vwap, 4),
            "current": round(current, 4),
            "deviation_pct": round(deviation_pct, 4),
            "above_vwap": current > vwap,
            "ticks_used": len(ticks),
        }

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
        """Iniciar feed. Intenta WebSocket con timeout, fallback a polling HTTP."""
        self._running = True
        print(f"[BinanceFeed] Iniciando feed para {self.pairs}", flush=True)
        # Intentar WebSocket con timeout de 15s para recibir primer dato
        try:
            ws_task = asyncio.create_task(self._run_websocket())
            # Esperar hasta 15s a que llegue al menos un precio
            for _ in range(15):
                await asyncio.sleep(1)
                if self._latest:
                    print(f"[BinanceFeed] WebSocket OK, precios recibidos: {list(self._latest.keys())}", flush=True)
                    await ws_task  # Continuar con WebSocket
                    return
            # Si en 15s no llegó nada, cancelar WS y usar polling
            ws_task.cancel()
            try:
                await ws_task
            except (asyncio.CancelledError, Exception):
                pass
            print("[BinanceFeed] WebSocket sin datos en 15s, cambiando a HTTP polling", flush=True)
        except (asyncio.CancelledError, Exception) as e:
            print(f"[BinanceFeed] WebSocket falló: {e}, cambiando a HTTP polling", flush=True)
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
                                # Muestrear 1 tick/segundo para historia larga
                                if ts - self._last_sample_ts.get(pair, 0) >= 1.0:
                                    self._sampled[pair].append(PriceTick(price, ts))
                                    self._last_sample_ts[pair] = ts
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
        """Fallback: polling HTTP cada 2 segundos para precio.
        Intenta múltiples fuentes: Binance.com, Binance.us, CoinGecko.
        """
        import httpx
        print("[BinanceFeed] Iniciando HTTP polling", flush=True)

        # Mapeo para CoinGecko
        COINGECKO_IDS = {"btcusdt": "bitcoin", "ethusdt": "ethereum", "solusdt": "solana", "xrpusdt": "ripple"}

        async with httpx.AsyncClient(timeout=10) as client:
            errors_in_a_row = 0
            source = "binance.com"
            while self._running:
                try:
                    got_any = False
                    for pair in self.pairs:
                        price = None

                        # Fuente 1: Binance.com
                        if source in ("binance.com", "all"):
                            try:
                                resp = await client.get(
                                    "https://api.binance.com/api/v3/ticker/price",
                                    params={"symbol": pair.upper()},
                                )
                                if resp.status_code == 200:
                                    price = float(resp.json().get("price", 0))
                            except Exception:
                                pass

                        # Fuente 2: Binance.us
                        if not price and source in ("binance.us", "all"):
                            try:
                                resp = await client.get(
                                    "https://api.binance.us/api/v3/ticker/price",
                                    params={"symbol": pair.upper()},
                                )
                                if resp.status_code == 200:
                                    price = float(resp.json().get("price", 0))
                            except Exception:
                                pass

                        # Fuente 3: CoinGecko
                        if not price:
                            try:
                                cg_id = COINGECKO_IDS.get(pair)
                                if cg_id:
                                    resp = await client.get(
                                        "https://api.coingecko.com/api/v3/simple/price",
                                        params={"ids": cg_id, "vs_currencies": "usd"},
                                    )
                                    if resp.status_code == 200:
                                        data = resp.json()
                                        price = float(data.get(cg_id, {}).get("usd", 0))
                            except Exception:
                                pass

                        if price and price > 0:
                            now = time.time()
                            self._history[pair].append(PriceTick(price, now))
                            self._latest[pair] = price
                            # Muestrear 1 tick/segundo para historia larga
                            if now - self._last_sample_ts.get(pair, 0) >= 1.0:
                                self._sampled[pair].append(PriceTick(price, now))
                                self._last_sample_ts[pair] = now
                            got_any = True

                    if got_any:
                        if errors_in_a_row > 0:
                            print(f"[BinanceFeed] Precios recuperados via {source}", flush=True)
                        errors_in_a_row = 0
                    else:
                        errors_in_a_row += 1
                        if errors_in_a_row == 1:
                            print(f"[BinanceFeed] Sin datos de {source}, probando todas las fuentes...", flush=True)
                            source = "all"
                        if errors_in_a_row % 30 == 0:
                            print(f"[BinanceFeed] {errors_in_a_row} polls sin datos", flush=True)

                except Exception as e:
                    errors_in_a_row += 1
                    if errors_in_a_row <= 3:
                        print(f"[BinanceFeed] Polling error: {e}", flush=True)
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
                    "sampled_ticks": len(self._sampled.get(p, [])),
                }
                for p in self.pairs
            },
        }
