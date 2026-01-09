"""Polymarket API client for fetching markets and trades."""
import httpx
from datetime import datetime
from typing import Optional
import structlog

from src.models import Market, Trade

logger = structlog.get_logger()

# API Base URLs
CLOB_API_URL = "https://clob.polymarket.com"
GAMMA_API_URL = "https://gamma-api.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"


class PolymarketClient:
    """Client for interacting with Polymarket APIs."""
    
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
    
    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()
    
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")
        return self._client
    
    async def get_markets(self, limit: int = 100, active_only: bool = True) -> list[Market]:
        """Fetch list of markets from Gamma API."""
        try:
            params = {"limit": limit}
            if active_only:
                params["active"] = "true"
                params["closed"] = "false"
            
            response = await self.client.get(
                f"{GAMMA_API_URL}/markets",
                params=params
            )
            response.raise_for_status()
            data = response.json()
            
            markets = []
            for item in data:
                try:
                    market = Market(
                        condition_id=item.get("conditionId", item.get("condition_id", "")),
                        question=item.get("question", ""),
                        slug=item.get("slug", ""),
                        icon=item.get("icon"),
                        active=item.get("active", True)
                    )
                    if market.condition_id:
                        markets.append(market)
                except Exception as e:
                    logger.warning("failed_to_parse_market", error=str(e), item=item)
            
            logger.info("fetched_markets", count=len(markets))
            return markets
            
        except httpx.HTTPError as e:
            logger.error("failed_to_fetch_markets", error=str(e))
            return []
    
    async def get_market_trades(self, condition_id: str, limit: int = 100) -> list[Trade]:
        """Fetch recent trades for a specific market."""
        try:
            response = await self.client.get(
                f"{CLOB_API_URL}/trades",
                params={
                    "market": condition_id,
                    "limit": limit
                }
            )
            response.raise_for_status()
            data = response.json()
            
            trades = []
            items = data if isinstance(data, list) else data.get("data", [])
            
            for item in items:
                try:
                    trade = self._parse_trade(item, condition_id)
                    if trade:
                        trades.append(trade)
                except Exception as e:
                    logger.warning("failed_to_parse_trade", error=str(e))
            
            return trades
            
        except httpx.HTTPError as e:
            logger.warning("failed_to_fetch_trades", market=condition_id, error=str(e))
            return []
    
    async def get_all_recent_trades(self, markets: list[Market], trades_per_market: int = 50) -> list[Trade]:
        """Fetch recent trades across multiple markets."""
        all_trades = []
        
        for market in markets:
            trades = await self.get_market_trades(market.condition_id, trades_per_market)
            for trade in trades:
                trade.market_question = market.question
                trade.market_slug = market.slug
            all_trades.extend(trades)
        
        # Sort by timestamp descending
        all_trades.sort(key=lambda t: t.timestamp, reverse=True)
        logger.info("fetched_all_trades", total=len(all_trades), markets=len(markets))
        
        return all_trades
    
    def _parse_trade(self, item: dict, condition_id: str) -> Optional[Trade]:
        """Parse a trade from API response."""
        try:
            # Handle different API response formats
            timestamp_str = item.get("timestamp") or item.get("matchTime") or item.get("created_at")
            if timestamp_str:
                if isinstance(timestamp_str, (int, float)):
                    timestamp = datetime.fromtimestamp(timestamp_str)
                else:
                    timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            else:
                timestamp = datetime.now()
            
            # Get wallet address
            wallet = (
                item.get("maker_address") or 
                item.get("taker_address") or 
                item.get("owner") or
                item.get("user", {}).get("address", "unknown")
            )
            
            # Get size (try different field names)
            size_str = item.get("size") or item.get("amount") or item.get("tradeAmount") or "0"
            size = float(size_str) if size_str else 0.0
            
            # Get price
            price_str = item.get("price") or item.get("avgPrice") or "0"
            price = float(price_str) if price_str else 0.0
            
            # Get side
            side = item.get("side") or item.get("outcome") or "YES"
            if side.lower() in ["buy", "long", "yes"]:
                side = "YES"
            elif side.lower() in ["sell", "short", "no"]:
                side = "NO"
            
            return Trade(
                transaction_hash=item.get("transactionHash") or item.get("id") or "",
                market_id=condition_id,
                market_question=item.get("market", {}).get("question", ""),
                market_slug=item.get("market", {}).get("slug", ""),
                wallet_address=wallet,
                side=side.upper(),
                size=size,
                price=price,
                timestamp=timestamp,
                outcome=item.get("outcome", side)
            )
        except Exception as e:
            logger.warning("trade_parse_error", error=str(e), item=item)
            return None
