"""Cliente de API de Polymarket — Gamma (mercados) + Data API (trades)."""
import httpx
import json
import re
from datetime import datetime
from typing import Optional
import structlog

from src.models import Market, Trade
from src import config

logger = structlog.get_logger()

# Tags de Gamma API que excluimos (deportes, crypto precio)
EXCLUDE_TAGS = {"sports", "nba", "nfl", "nhl", "mlb", "mls", "soccer", "esports",
                "crypto-prices", "crypto-price", "updown"}

# Regex de respaldo para filtrar por título
EXCLUDE_TITLE_RE = re.compile(
    r'\b(?:vs\.?|Spread:|Points|Goals|NBA|NFL|NHL|MLB|MLS|'
    r'Up or Down|updown|price of|above \$|below \$|close above|close below|'
    r'all-time high|ATH)\b'
    r'|\d+[AP]M.*ET',
    re.IGNORECASE,
)


def is_insider_relevant(title: str, tags: list[str] | None = None) -> bool:
    """Verifica si el mercado es relevante para insider info."""
    if not title:
        return False
    if tags:
        lower_tags = {t.lower() for t in tags}
        if lower_tags & EXCLUDE_TAGS:
            return False
    if EXCLUDE_TITLE_RE.search(title):
        return False
    return True


class PolymarketClient:
    """Cliente para Gamma API (mercados) y Data API (trades)."""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._market_cache: dict[str, dict] = {}

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Cliente no inicializado. Usa 'async with'.")
        return self._client

    # ── Trades ────────────────────────────────────────────────────────

    async def get_recent_trades(self, limit: int = 200) -> list[Trade]:
        """Obtener trades recientes, filtrados y enriquecidos."""
        try:
            fetch_limit = limit * 4
            response = await self.client.get(
                f"{config.DATA_API_URL}/trades",
                params={"limit": fetch_limit},
            )
            response.raise_for_status()
            data = response.json()

            trades: list[Trade] = []
            filtered_cat = 0
            size_skip = 0
            no_id = 0

            for item in data:
                try:
                    # Trades no tienen title/tags — buscar en market cache
                    cid = item.get("conditionId", item.get("market", item.get("condition_id", "")))
                    if not cid:
                        no_id += 1
                        continue

                    # Filtrar por categoría usando market cache
                    market_data = self._market_cache.get(cid)
                    if market_data:
                        title = market_data.get("question", "")
                        cat = market_data.get("category", "")
                        tags = [cat] if cat else []
                        if not is_insider_relevant(title, tags):
                            filtered_cat += 1
                            continue
                    # Si no hay cache, no filtrar (beneficio de la duda)

                    trade = self._parse_trade(item)
                    if trade:
                        trades.append(trade)

                    if len(trades) >= limit:
                        break
                except Exception as e:
                    logger.warning("error_parseando_trade", error=str(e))

            print(
                f"Trades: {len(trades)} parseados, "
                f"{filtered_cat} filtrados (categoría), {size_skip} filtrados (size), "
                f"{no_id} sin ID",
                flush=True,
            )
            return trades

        except httpx.HTTPError as e:
            logger.error("error_obteniendo_trades", error=str(e))
            return []

    # ── Markets ───────────────────────────────────────────────────────

    async def get_markets(self, limit: int = 100, active_only: bool = True) -> list[Market]:
        """Obtener mercados desde Gamma API."""
        try:
            params: dict = {"limit": limit, "_order": "volume24hr", "_sort": "desc"}
            if active_only:
                params["active"] = "true"
                params["closed"] = "false"

            response = await self.client.get(
                f"{config.GAMMA_API_URL}/markets", params=params,
            )
            response.raise_for_status()
            data = response.json()

            markets: list[Market] = []
            for item in data:
                try:
                    cid = item.get("conditionId", item.get("condition_id", ""))
                    if not cid:
                        continue
                    end_str = item.get("endDate") or item.get("end_date_iso")
                    end_date = None
                    if end_str:
                        try:
                            end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                        except Exception:
                            pass

                    tags = item.get("tags", [])
                    category = tags[0] if tags else None

                    market = Market(
                        condition_id=cid,
                        question=item.get("question", ""),
                        slug=item.get("slug", ""),
                        icon=item.get("icon"),
                        end_date=end_date,
                        active=item.get("active", True),
                        category=category,
                        volume_24h=float(item.get("volume24hr", 0) or 0),
                        liquidity=float(item.get("liquidity", 0) or 0),
                    )
                    # Cache para enriquecer trades
                    self._market_cache[cid] = {
                        "question": market.question,
                        "slug": market.slug,
                        "end_date": market.end_date,
                        "category": market.category,
                    }
                    markets.append(market)
                except Exception as e:
                    logger.warning("error_parseando_mercado", error=str(e))

            logger.info("mercados_obtenidos", count=len(markets))
            return markets

        except httpx.HTTPError as e:
            logger.error("error_obteniendo_mercados", error=str(e))
            return []

    async def get_market_by_id(self, condition_id: str) -> Optional[dict]:
        """Obtener datos de un mercado específico (con cache)."""
        if condition_id in self._market_cache:
            return self._market_cache[condition_id]
        try:
            response = await self.client.get(
                f"{config.GAMMA_API_URL}/markets",
                params={"condition_id": condition_id},
            )
            if response.status_code == 200:
                data = response.json()
                item = data[0] if isinstance(data, list) and len(data) > 0 else data if isinstance(data, dict) else None
                if not item:
                    return None
                end_str = item.get("endDate") or item.get("end_date_iso")
                end_date = None
                if end_str:
                    try:
                        end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    except Exception:
                        pass
                tags = item.get("tags", [])
                market_info = {
                    "question": item.get("question", ""),
                    "slug": item.get("slug", ""),
                    "end_date": end_date,
                    "category": tags[0] if tags else None,
                }
                self._market_cache[condition_id] = market_info
                return market_info
        except Exception:
            pass
        return None

    async def check_market_resolution(self, condition_id: str) -> Optional[str]:
        """Verificar si un mercado se resolvió. Devuelve 'Yes'/'No'/None."""
        try:
            # Gamma API usa query params, devuelve array
            response = await self.client.get(
                f"{config.GAMMA_API_URL}/markets",
                params={"condition_id": condition_id},
            )
            if response.status_code == 200:
                data = response.json()
                # Puede ser array o dict
                item = data[0] if isinstance(data, list) and len(data) > 0 else data if isinstance(data, dict) else None
                if not item:
                    return None
                is_resolved = item.get("resolved") == True or item.get("closed") == True
                if is_resolved:
                    # Buscar resultado en varios campos posibles
                    outcome = (
                        item.get("outcome") or
                        item.get("resolution") or
                        item.get("winningOutcome") or
                        ""
                    )
                    if outcome:
                        return str(outcome)
                    # Si está cerrado pero sin outcome explícito, intentar con outcomePrices
                    prices_str = item.get("outcomePrices", "")
                    if prices_str:
                        try:
                            prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
                            if isinstance(prices, list) and len(prices) >= 2:
                                if float(prices[0]) >= 0.95:
                                    return "Yes"
                                elif float(prices[1]) >= 0.95:
                                    return "No"
                        except Exception:
                            pass
        except Exception as e:
            logger.warning("error_check_resolution", condition_id=condition_id[:20], error=str(e))
        return None

    # ── Market Price (para price impact) ─────────────────────────────

    async def get_market_price(self, condition_id: str, outcome: str = "Yes") -> Optional[float]:
        """Obtener precio actual de un outcome en un mercado."""
        try:
            response = await self.client.get(
                f"{config.GAMMA_API_URL}/markets",
                params={"condition_id": condition_id},
            )
            if response.status_code == 200:
                data = response.json()
                item = data[0] if isinstance(data, list) and len(data) > 0 else data if isinstance(data, dict) else None
                if not item:
                    return None
                # outcomePrices es un string como "[0.65, 0.35]"
                prices_str = item.get("outcomePrices", "")
                if prices_str:
                    try:
                        prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
                        if isinstance(prices, list) and len(prices) >= 2:
                            return float(prices[0]) if outcome == "Yes" else float(prices[1])
                    except Exception:
                        pass
                # Fallback: usar bestAsk/bestBid
                if outcome == "Yes":
                    return float(item.get("bestAsk", 0) or item.get("lastTradePrice", 0) or 0)
        except Exception:
            pass
        return None

    # ── Parse ─────────────────────────────────────────────────────────

    def _parse_trade(self, item: dict) -> Optional[Trade]:
        try:
            # Parsear timestamp (puede ser int, float, string)
            ts_val = item.get("timestamp") or item.get("match_time") or item.get("created_at")
            if isinstance(ts_val, (int, float)):
                if ts_val > 1e12:  # milliseconds
                    ts_val = ts_val / 1000
                timestamp = datetime.fromtimestamp(ts_val)
            elif isinstance(ts_val, str):
                try:
                    timestamp = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
                except Exception:
                    timestamp = datetime.now()
            else:
                timestamp = datetime.now()

            cid = item.get("conditionId", "")
            market_data = self._market_cache.get(cid, {})

            return Trade(
                transaction_hash=item.get("transactionHash", item.get("id", "")),
                market_id=cid,
                market_question=item.get("title", market_data.get("question", "")),
                market_slug=item.get("eventSlug", item.get("slug", market_data.get("slug", ""))),
                wallet_address=item.get("proxyWallet", item.get("maker_address", "unknown")),
                side=item.get("side", "BUY"),
                size=float(item.get("size", 0)),
                price=float(item.get("price", 0)),
                timestamp=timestamp,
                outcome=item.get("outcome", "Yes"),
                market_end_date=market_data.get("end_date"),
                market_category=market_data.get("category"),
                trader_name=item.get("name") or None,
                trader_pseudonym=item.get("pseudonym") or None,
                trader_profile_image=item.get("profileImage") or item.get("profileImageOptimized") or None,
            )
        except Exception as e:
            logger.warning("error_parse_trade", error=str(e))
            return None
