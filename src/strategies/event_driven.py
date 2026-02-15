"""Event-Driven Bot — Tradear basado en datos de APIs de resultados."""
import time
import httpx
from src import config


class EventDrivenBot:
    """Bot que monitorea APIs de resultados y tradea antes de resolución oficial."""

    def __init__(self, db):
        self.db = db
        self._last_check = 0
        self._signals_today = 0
        self._daily_reset = 0

    async def tick(self):
        """Ejecutar un ciclo de detección de eventos."""
        if not config.FEATURE_EVENT_DRIVEN:
            return []

        now = time.time()
        if now - self._last_check < config.ED_CHECK_INTERVAL:
            return []
        self._last_check = now

        # Reset diario
        if now - self._daily_reset > 86400:
            self._signals_today = 0
            self._daily_reset = now

        signals = []
        for source in config.ED_SOURCES:
            try:
                if source == "crypto_prices":
                    s = await self._check_crypto_prices()
                    if s:
                        signals.extend(s)
                elif source == "uma_oracle":
                    s = await self._check_uma_oracle()
                    if s:
                        signals.extend(s)
            except Exception as e:
                print(f"Error en event source {source}: {e}", flush=True)

        return signals

    async def _check_crypto_prices(self) -> list:
        """Verificar precios crypto contra mercados de Polymarket activos."""
        signals = []
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Obtener precio actual de BTC
                r = await client.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
                if r.status_code == 200:
                    btc_price = float(r.json()["price"])
                    # Buscar mercados de resolución de precio BTC
                    markets = await self.db.get_markets_by_category("crypto-prices")
                    for m in (markets or []):
                        q = (m.get("question") or "").lower()
                        # Detectar mercados tipo "BTC above $X"
                        if "btc" in q and "above" in q:
                            try:
                                # Extraer el threshold del nombre
                                import re
                                nums = re.findall(r'\$?([\d,]+)', q)
                                if nums:
                                    threshold = float(nums[-1].replace(",", ""))
                                    current_price = m.get("price", 0.5)
                                    # Si BTC está claramente arriba/abajo del threshold
                                    if btc_price > threshold * 1.02 and current_price < 0.90:
                                        edge = (1.0 - current_price) * 100
                                        if edge >= config.ED_MIN_EDGE_PCT:
                                            signals.append({
                                                "source": "crypto_prices",
                                                "market_id": m.get("condition_id", ""),
                                                "action": "buy_yes",
                                                "edge_pct": round(edge, 2),
                                                "reason": f"BTC ${btc_price:.0f} > threshold ${threshold:.0f}",
                                                "timestamp": time.time(),
                                            })
                                    elif btc_price < threshold * 0.98 and current_price > 0.10:
                                        edge = current_price * 100
                                        if edge >= config.ED_MIN_EDGE_PCT:
                                            signals.append({
                                                "source": "crypto_prices",
                                                "market_id": m.get("condition_id", ""),
                                                "action": "buy_no",
                                                "edge_pct": round(edge, 2),
                                                "reason": f"BTC ${btc_price:.0f} < threshold ${threshold:.0f}",
                                                "timestamp": time.time(),
                                            })
                            except (ValueError, IndexError):
                                continue
        except Exception as e:
            print(f"Error checking crypto prices: {e}", flush=True)
        return signals

    async def _check_uma_oracle(self) -> list:
        """Verificar resoluciones pendientes en UMA Oracle."""
        # UMA Oracle verifica resoluciones de mercados
        # Cuando hay una propuesta de resolución, el precio debería moverse
        signals = []
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    "https://oracle.uma.xyz/api/queries",
                    params={"chainId": 137, "isActive": True}
                )
                if r.status_code == 200:
                    queries = r.json()
                    for q in (queries or [])[:10]:
                        # Si hay una propuesta activa con precio propuesto
                        proposed = q.get("proposedPrice")
                        if proposed is not None:
                            signals.append({
                                "source": "uma_oracle",
                                "query_id": q.get("queryId", ""),
                                "proposed_price": proposed,
                                "timestamp": time.time(),
                            })
        except Exception:
            pass
        return signals

    def get_stats(self) -> dict:
        return {
            "enabled": config.FEATURE_EVENT_DRIVEN,
            "signals_today": self._signals_today,
            "sources": config.ED_SOURCES,
            "min_edge_pct": config.ED_MIN_EDGE_PCT,
        }
