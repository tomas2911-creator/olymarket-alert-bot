"""News Fetcher — Obtener noticias relevantes para mercados de Polymarket."""
import time
import re
import httpx
from src import config


class NewsFetcher:
    """Busca y almacena noticias relevantes para mercados activos."""

    def __init__(self, db):
        self.db = db
        self._cache = {}  # query -> {articles, ts}
        self._last_fetch = 0
        self._fetch_interval = 900  # 15 min entre scans

    async def fetch_news_for_markets(self, markets: list) -> list:
        """Buscar noticias para los mercados activos más relevantes."""
        now = time.time()
        if now - self._last_fetch < self._fetch_interval:
            return []

        self._last_fetch = now
        all_articles = []

        # Extraer keywords de los top mercados por volumen
        for market in (markets or [])[:20]:
            question = market.get("question", "") or ""
            market_id = market.get("condition_id", "")
            if not question or not market_id:
                continue

            keywords = self._extract_keywords(question)
            if not keywords:
                continue

            query = " OR ".join(keywords[:3])
            articles = await self._search_news(query, market_id, question)
            all_articles.extend(articles)

        # Guardar en DB
        saved = 0
        for article in all_articles:
            try:
                if await self.db.save_news_item(article):
                    saved += 1
            except Exception:
                pass

        if saved:
            print(f"[NewsFetcher] {len(all_articles)} artículos encontrados, {saved} nuevos guardados", flush=True)

        return all_articles

    async def _search_news(self, query: str, market_id: str, market_question: str) -> list:
        """Buscar noticias via Google News RSS (gratis, sin API key)."""
        cache_key = query.lower()
        now = time.time()

        # Cache de 30 min
        if cache_key in self._cache and now - self._cache[cache_key]["ts"] < 1800:
            return self._cache[cache_key]["articles"]

        articles = []
        try:
            # Google News RSS — gratis y sin límite
            url = f"https://news.google.com/rss/search?q={query}&hl=en&gl=US&ceid=US:en"
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    articles = self._parse_rss(r.text, market_id, market_question, query)
        except Exception as e:
            print(f"[NewsFetcher] Error buscando '{query}': {e}", flush=True)

        # Si hay NewsAPI key, complementar
        if config.NEWS_API_KEY and len(articles) < 5:
            try:
                api_articles = await self._search_newsapi(query, market_id, market_question)
                articles.extend(api_articles)
            except Exception:
                pass

        self._cache[cache_key] = {"articles": articles[:10], "ts": now}
        return articles[:10]

    async def _search_newsapi(self, query: str, market_id: str, market_question: str) -> list:
        """Buscar en NewsAPI (requiere API key)."""
        articles = []
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                from_date = time.strftime(
                    "%Y-%m-%dT%H:%M:%S",
                    time.gmtime(time.time() - 24 * 3600)
                )
                r = await client.get(
                    "https://newsapi.org/v2/everything",
                    params={
                        "q": query,
                        "from": from_date,
                        "sortBy": "publishedAt",
                        "language": "en",
                        "pageSize": 10,
                        "apiKey": config.NEWS_API_KEY,
                    }
                )
                if r.status_code == 200:
                    data = r.json()
                    for item in (data.get("articles") or [])[:10]:
                        articles.append({
                            "title": item.get("title", ""),
                            "source": item.get("source", {}).get("name", ""),
                            "url": item.get("url", ""),
                            "published_at": item.get("publishedAt", ""),
                            "market_id": market_id,
                            "market_question": market_question,
                            "query": query,
                        })
        except Exception:
            pass
        return articles

    def _parse_rss(self, xml_text: str, market_id: str, market_question: str, query: str) -> list:
        """Parsear RSS de Google News (formato XML simple)."""
        articles = []
        # Parseo simple sin dependencias XML
        items = re.findall(r'<item>(.*?)</item>', xml_text, re.DOTALL)
        for item in items[:10]:
            title_match = re.search(r'<title>(.*?)</title>', item)
            link_match = re.search(r'<link>(.*?)</link>', item)
            source_match = re.search(r'<source[^>]*>(.*?)</source>', item)
            pub_match = re.search(r'<pubDate>(.*?)</pubDate>', item)

            title = title_match.group(1) if title_match else ""
            # Limpiar CDATA y HTML
            title = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', title)
            title = re.sub(r'<[^>]+>', '', title)

            if not title:
                continue

            articles.append({
                "title": title,
                "source": source_match.group(1) if source_match else "Google News",
                "url": link_match.group(1) if link_match else "",
                "published_at": pub_match.group(1) if pub_match else "",
                "market_id": market_id,
                "market_question": market_question,
                "query": query,
            })
        return articles

    def _extract_keywords(self, question: str) -> list:
        """Extraer keywords relevantes de una pregunta de mercado."""
        stop_words = {
            "will", "the", "a", "an", "in", "on", "at", "to", "by", "of",
            "is", "be", "this", "that", "it", "for", "with", "as", "was",
            "are", "were", "been", "being", "have", "has", "had", "do",
            "does", "did", "but", "and", "or", "not", "no", "yes", "if",
            "then", "than", "when", "where", "who", "what", "which", "how",
            "before", "after", "above", "below", "between", "during",
            "more", "most", "less", "least", "any", "all", "each", "every",
            "win", "price", "market", "above", "reach",
        }
        words = question.lower().replace("?", "").replace("'", "").replace('"', '').split()
        keywords = [w for w in words if w not in stop_words and len(w) > 2]
        # Priorizar nombres propios (capitalizados en el original)
        original_words = question.replace("?", "").split()
        proper_nouns = [w for w in original_words if len(w) > 2 and w[0].isupper() and w.lower() not in stop_words]
        if proper_nouns:
            return proper_nouns[:5]
        return keywords[:5]

    def get_stats(self) -> dict:
        return {
            "cached_queries": len(self._cache),
            "last_fetch_ago": round(time.time() - self._last_fetch) if self._last_fetch else None,
            "has_newsapi_key": bool(config.NEWS_API_KEY),
        }
