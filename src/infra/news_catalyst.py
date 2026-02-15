"""News Catalyst Signal #19 — Monitorear noticias relacionadas con mercados."""
import time
import httpx
from src import config


class NewsCatalyst:
    """Detecta picos de noticias relacionadas con mercados de Polymarket."""

    def __init__(self):
        self._cache = {}  # keyword -> {count, last_check}
        self._last_scan = 0

    async def check_news_for_market(self, market_question: str) -> dict:
        """Verificar si hay noticias recientes relacionadas con un mercado."""
        if not config.FEATURE_NEWS_CATALYST or not config.NEWS_API_KEY:
            return {"has_catalyst": False, "mentions": 0}

        # Extraer keywords del mercado
        keywords = self._extract_keywords(market_question)
        if not keywords:
            return {"has_catalyst": False, "mentions": 0}

        query = " OR ".join(keywords[:3])
        cache_key = query.lower()

        # Cache de 30 min
        now = time.time()
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if now - cached["last_check"] < 1800:
                return cached["result"]

        try:
            mentions = await self._search_news(query)
            result = {
                "has_catalyst": mentions >= config.NEWS_MIN_MENTIONS,
                "mentions": mentions,
                "query": query,
            }
            self._cache[cache_key] = {"result": result, "last_check": now}
            return result
        except Exception as e:
            print(f"Error buscando noticias: {e}", flush=True)
            return {"has_catalyst": False, "mentions": 0}

    async def _search_news(self, query: str) -> int:
        """Buscar noticias en NewsAPI."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                from_date = time.strftime(
                    "%Y-%m-%dT%H:%M:%S",
                    time.gmtime(time.time() - config.NEWS_LOOKBACK_HOURS * 3600)
                )
                r = await client.get(
                    "https://newsapi.org/v2/everything",
                    params={
                        "q": query,
                        "from": from_date,
                        "sortBy": "publishedAt",
                        "language": "en",
                        "pageSize": 20,
                        "apiKey": config.NEWS_API_KEY,
                    }
                )
                if r.status_code == 200:
                    data = r.json()
                    return data.get("totalResults", 0)
        except Exception:
            pass
        return 0

    def _extract_keywords(self, question: str) -> list:
        """Extraer palabras clave de una pregunta de mercado."""
        stop_words = {
            "will", "the", "a", "an", "in", "on", "at", "to", "by", "of",
            "is", "be", "this", "that", "it", "for", "with", "as", "was",
            "are", "were", "been", "being", "have", "has", "had", "do",
            "does", "did", "but", "and", "or", "not", "no", "yes", "if",
            "then", "than", "when", "where", "who", "what", "which", "how",
            "before", "after", "above", "below", "between", "during",
        }
        words = question.lower().replace("?", "").replace("'", "").split()
        keywords = [w for w in words if w not in stop_words and len(w) > 2]
        return keywords[:5]

    def get_stats(self) -> dict:
        return {
            "enabled": config.FEATURE_NEWS_CATALYST,
            "has_api_key": bool(config.NEWS_API_KEY),
            "cached_queries": len(self._cache),
            "points": config.NEWS_CATALYST_POINTS,
        }
