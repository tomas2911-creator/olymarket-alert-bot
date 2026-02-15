"""Cross-Platform Arb — Comparar precios entre prediction markets."""
import time
import httpx
from src import config


class CrossPlatformArb:
    """Detecta oportunidades de arbitraje entre Polymarket y otros markets."""

    def __init__(self, db):
        self.db = db
        self._cache = {}  # source -> {event -> price}
        self._last_scan = 0
        self._signals = []

    async def tick(self):
        """Escanear plataformas buscando diferencias de precio."""
        if not config.FEATURE_CROSS_PLATFORM:
            return []

        now = time.time()
        if now - self._last_scan < 120:  # Escanear cada 2 min
            return []
        self._last_scan = now

        signals = []
        poly_markets = await self._get_polymarket_prices()

        for source in config.CROSS_PLATFORM_SOURCES:
            try:
                ext_markets = await self._get_external_prices(source)
                matches = self._find_matches(poly_markets, ext_markets, source)
                signals.extend(matches)
            except Exception as e:
                print(f"Error en cross-platform {source}: {e}", flush=True)

        self._signals = signals
        return signals

    async def _get_polymarket_prices(self) -> dict:
        """Obtener precios actuales de mercados activos en Polymarket."""
        try:
            markets = await self.db.get_active_markets()
            result = {}
            for m in (markets or []):
                q = (m.get("question") or "").lower().strip()
                if q:
                    result[q] = {
                        "price": m.get("price", 0),
                        "market_id": m.get("condition_id", ""),
                        "slug": m.get("slug", ""),
                    }
            return result
        except Exception:
            return {}

    async def _get_external_prices(self, source: str) -> dict:
        """Obtener precios de una plataforma externa."""
        result = {}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                if source == "manifold":
                    r = await client.get("https://api.manifold.markets/v0/markets?limit=50&sort=liquidity")
                    if r.status_code == 200:
                        for m in r.json():
                            q = (m.get("question") or "").lower().strip()
                            prob = m.get("probability", 0)
                            if q and prob > 0:
                                result[q] = {"price": prob, "url": m.get("url", ""), "id": m.get("id", "")}
                # Kalshi y PredictIt requieren API keys — stub para cuando se configuren
                elif source == "kalshi":
                    pass  # Requiere API key de Kalshi
                elif source == "predictit":
                    pass  # PredictIt cerró para nuevos mercados
        except Exception as e:
            print(f"Error fetching {source}: {e}", flush=True)
        return result

    def _find_matches(self, poly: dict, external: dict, source: str) -> list:
        """Encontrar mercados coincidentes con diferencia de precio."""
        signals = []
        for q_poly, p_poly in poly.items():
            for q_ext, p_ext in external.items():
                # Match simple por similitud de texto
                if self._similar(q_poly, q_ext):
                    poly_price = p_poly["price"]
                    ext_price = p_ext["price"]
                    diff = abs(poly_price - ext_price) * 100

                    if diff >= config.CROSS_PLATFORM_MIN_EDGE:
                        signals.append({
                            "polymarket_question": q_poly[:80],
                            "external_question": q_ext[:80],
                            "source": source,
                            "poly_price": round(poly_price, 4),
                            "ext_price": round(ext_price, 4),
                            "edge_pct": round(diff, 2),
                            "action": "buy_poly" if poly_price < ext_price else "sell_poly",
                            "market_id": p_poly["market_id"],
                            "timestamp": time.time(),
                        })
        return signals

    def _similar(self, a: str, b: str) -> bool:
        """Verificar si dos preguntas son sobre el mismo evento."""
        # Tokenizar y comparar overlap
        words_a = set(a.split())
        words_b = set(b.split())
        # Remover stop words
        stop = {"will", "the", "a", "an", "in", "on", "at", "to", "by", "of", "is", "be", "?"}
        words_a -= stop
        words_b -= stop
        if not words_a or not words_b:
            return False
        overlap = len(words_a & words_b) / min(len(words_a), len(words_b))
        return overlap >= 0.6

    def get_stats(self) -> dict:
        return {
            "enabled": config.FEATURE_CROSS_PLATFORM,
            "signals": len(self._signals),
            "sources": config.CROSS_PLATFORM_SOURCES,
            "min_edge": config.CROSS_PLATFORM_MIN_EDGE,
        }

    def get_signals(self) -> list:
        return self._signals
