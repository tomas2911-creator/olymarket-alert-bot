"""Cliente de API de Polymarket — Gamma (mercados) + Data API (trades)."""
import httpx
import json
import re
from datetime import datetime
from typing import Optional
import structlog

from src.models import Market, Trade
from src import config
from src.infra.rate_limiter import get_limiter

logger = structlog.get_logger()

# Tags de Gamma API que excluimos (deportes, crypto precio)
EXCLUDE_TAGS = {"sports", "nba", "nfl", "nhl", "mlb", "mls", "soccer", "esports",
                "crypto-prices", "crypto-price", "updown"}

# Regex de respaldo para filtrar por título
EXCLUDE_TITLE_RE = re.compile(
    r'\b(?:vs\.?|Spread:|Points|Goals|NBA|NFL|NHL|MLB|MLS|'
    r'Up or Down|updown|price of|above \$|below \$|close above|close below|'
    r'all-time high|ATH|Over/Under|O/U|Total Kills|'
    r'win on \d{4}-\d{2}-\d{2}|FC\b|United\b|Wildcats|Panthers|'
    r'Lakers|Warriors|Celtics|Cowboys|Eagles|Chiefs|'
    r'Premier League|Champions League|La Liga|Serie A|Bundesliga|'
    r'Ligue 1|World Cup|Copa America|Euro \d{4}|UFC|'
    r'Grand Prix|Formula 1|F1\b|ATP|WTA|Open \d{4})\b'
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

    async def _rate_limited_get(self, url: str, **kwargs) -> httpx.Response:
        """GET con rate limiting y backoff automático."""
        limiter = get_limiter()
        await limiter.acquire()
        response = await self.client.get(url, **kwargs)
        if response.status_code == 429 or response.status_code >= 500:
            limiter.report_error(response.status_code)
        else:
            limiter.report_success()
        return response

    # ── Trades ────────────────────────────────────────────────────────

    async def get_recent_trades(self, limit: int = 200) -> list[Trade]:
        """Obtener trades recientes, filtrados y enriquecidos."""
        try:
            fetch_limit = limit * 4
            response = await self._rate_limited_get(
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

                    # Filtrar por categoría usando market cache o título del trade
                    market_data = self._market_cache.get(cid)
                    filter_title = ""
                    filter_tags = []
                    if market_data:
                        filter_title = market_data.get("question", "")
                        cat = market_data.get("category", "")
                        filter_tags = [cat] if cat else []
                    else:
                        # Sin cache: usar título directo del trade (Data API lo incluye)
                        filter_title = item.get("title", "")
                    if filter_title and not is_insider_relevant(filter_title, filter_tags):
                        filtered_cat += 1
                        continue

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
            params: dict = {"limit": limit, "order": "volume24hr", "ascending": "false"}
            if active_only:
                params["active"] = "true"
                params["closed"] = "false"

            response = await self._rate_limited_get(
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
        """Obtener datos de un mercado específico (con cache) via CLOB API."""
        if condition_id in self._market_cache:
            return self._market_cache[condition_id]
        try:
            response = await self.client.get(
                f"{config.CLOB_API_URL}/markets/{condition_id}",
            )
            if response.status_code == 200:
                item = response.json()
                if not item or not item.get("question"):
                    return None
                end_str = item.get("end_date_iso")
                end_date = None
                if end_str:
                    try:
                        end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    except Exception:
                        pass
                tags = item.get("tags", [])
                market_info = {
                    "question": item.get("question", ""),
                    "slug": item.get("market_slug", ""),
                    "end_date": end_date,
                    "category": tags[0] if tags else None,
                }
                self._market_cache[condition_id] = market_info
                return market_info
        except Exception:
            pass
        return None

    async def check_market_resolution(self, condition_id: str) -> Optional[str]:
        """Verificar si un mercado se resolvió. Devuelve el outcome ganador o None."""
        try:
            # CLOB API acepta conditionId directamente y tiene tokens[].winner
            response = await self.client.get(
                f"{config.CLOB_API_URL}/markets/{condition_id}",
            )
            if response.status_code == 200:
                data = response.json()
                if not data.get("closed"):
                    return None  # Mercado aún abierto
                # Buscar token ganador
                tokens = data.get("tokens", [])
                for token in tokens:
                    if token.get("winner") is True:
                        return str(token.get("outcome", "Yes"))
                # Si cerrado pero sin winner explícito, verificar por precio
                for token in tokens:
                    if float(token.get("price", 0)) >= 0.95:
                        return str(token.get("outcome", "Yes"))
        except Exception as e:
            logger.warning("error_check_resolution", condition_id=condition_id[:20], error=str(e))
        return None

    # ── Market Price (para price impact) ─────────────────────────────

    async def get_market_price(self, condition_id: str, outcome: str = "Yes") -> Optional[float]:
        """Obtener precio actual de un outcome en un mercado via CLOB API."""
        try:
            response = await self.client.get(
                f"{config.CLOB_API_URL}/markets/{condition_id}",
            )
            if response.status_code == 200:
                data = response.json()
                tokens = data.get("tokens", [])
                # Buscar token que matchea el outcome pedido
                for token in tokens:
                    if token.get("outcome", "").lower() == outcome.lower():
                        return float(token.get("price", 0))
                # Fallback: primer token = Yes, segundo = No
                if tokens:
                    idx = 0 if outcome == "Yes" else (1 if len(tokens) > 1 else 0)
                    return float(tokens[idx].get("price", 0))
        except Exception:
            pass
        return None

    # ── Order Book Depth ────────────────────────────────────────────

    async def get_orderbook_depth(self, token_id: str) -> Optional[dict]:
        """Obtener profundidad del order book via CLOB API.
        Devuelve total de liquidez en bids y asks.
        """
        try:
            response = await self.client.get(
                f"{config.CLOB_API_URL}/book",
                params={"token_id": token_id},
            )
            if response.status_code == 200:
                data = response.json()
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                total_bids = sum(float(b.get("size", 0)) for b in bids)
                total_asks = sum(float(a.get("size", 0)) for a in asks)
                return {
                    "total_bids": total_bids,
                    "total_asks": total_asks,
                    "total_liquidity": total_bids + total_asks,
                    "bid_levels": len(bids),
                    "ask_levels": len(asks),
                }
        except Exception:
            pass
        return None

    def calc_depth_impact(self, trade_size: float, book: dict) -> float:
        """Calcular qué % del order book consume un trade."""
        total = book.get("total_liquidity", 0)
        if total <= 0:
            return 0
        return (trade_size / total) * 100

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
                size=float(item.get("size", 0)) * float(item.get("price", 0)),
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
