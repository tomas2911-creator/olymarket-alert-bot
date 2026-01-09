"""Cliente de API de Polymarket para obtener mercados y trades."""
import httpx
from datetime import datetime
from typing import Optional
import structlog

from src.models import Market, Trade

logger = structlog.get_logger()

# URLs base de API
GAMMA_API_URL = "https://gamma-api.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"


class PolymarketClient:
    """Cliente para interactuar con las APIs de Polymarket."""
    
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
            raise RuntimeError("Cliente no inicializado. Usa 'async with'.")
        return self._client
    
    async def get_recent_trades(self, limit: int = 100) -> list[Trade]:
        """Obtener trades recientes de toda la plataforma (endpoint público)."""
        try:
            response = await self.client.get(
                f"{DATA_API_URL}/trades",
                params={"limit": limit}
            )
            response.raise_for_status()
            data = response.json()
            
            trades = []
            for item in data:
                try:
                    trade = self._parse_trade(item)
                    if trade:
                        trades.append(trade)
                except Exception as e:
                    logger.warning("error_parseando_trade", error=str(e))
            
            logger.info("trades_obtenidos", count=len(trades))
            return trades
            
        except httpx.HTTPError as e:
            logger.error("error_obteniendo_trades", error=str(e))
            return []
    
    async def get_markets(self, limit: int = 100, active_only: bool = True) -> list[Market]:
        """Obtener lista de mercados desde Gamma API."""
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
                    logger.warning("error_parseando_mercado", error=str(e))
            
            logger.info("mercados_obtenidos", count=len(markets))
            return markets
            
        except httpx.HTTPError as e:
            logger.error("error_obteniendo_mercados", error=str(e))
            return []
    
    def _parse_trade(self, item: dict) -> Optional[Trade]:
        """Parsear un trade desde la respuesta de la API."""
        try:
            # Timestamp (viene como unix timestamp)
            timestamp_val = item.get("timestamp")
            if isinstance(timestamp_val, (int, float)):
                timestamp = datetime.fromtimestamp(timestamp_val)
            else:
                timestamp = datetime.now()
            
            # Wallet address
            wallet = item.get("proxyWallet", "unknown")
            
            # Size en USD
            size = float(item.get("size", 0))
            
            # Price
            price = float(item.get("price", 0))
            
            # Side (BUY/SELL -> YES/NO)
            side = item.get("side", "BUY")
            outcome = item.get("outcome", "Yes")
            
            return Trade(
                transaction_hash=item.get("transactionHash", ""),
                market_id=item.get("conditionId", ""),
                market_question=item.get("title", ""),
                market_slug=item.get("slug", ""),
                wallet_address=wallet,
                side=side,
                size=size,
                price=price,
                timestamp=timestamp,
                outcome=outcome
            )
        except Exception as e:
            logger.warning("error_parse_trade", error=str(e), item=item)
            return None
