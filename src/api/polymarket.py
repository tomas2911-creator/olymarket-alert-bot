"""Cliente de API de Polymarket para obtener mercados y trades."""
import httpx
import re
from datetime import datetime
from typing import Optional
import structlog

from src.models import Market, Trade

logger = structlog.get_logger()

# URLs base de API
GAMMA_API_URL = "https://gamma-api.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"

# Palabras clave para EXCLUIR (deportes, crypto)
EXCLUDE_KEYWORDS = [
    # Deportes
    r'\bNBA\b', r'\bNFL\b', r'\bNHL\b', r'\bMLB\b', r'\bMLS\b',
    r'\bvs\.?\b', r'\bSpread:', r'\bPoints\b', r'\bGoals\b',
    r'Lakers', r'Warriors', r'Celtics', r'Knicks', r'Bulls', r'Heat',
    r'Mavericks', r'Nuggets', r'Clippers', r'Nets', r'Hawks', r'Jazz',
    r'Grizzlies', r'Pelicans', r'Cavaliers', r'Bucks', r'Pistons',
    r'Chiefs', r'Eagles', r'Cowboys', r'Patriots', r'Packers',
    r'Raiders', r'Broncos', r'Chargers', r'Ravens', r'Steelers',
    r'49ers', r'Seahawks', r'Cardinals', r'Rams', r'Lions', r'Bears',
    r'Vikings', r'Saints', r'Falcons', r'Panthers', r'Buccaneers',
    r'Jets', r'Bills', r'Dolphins', r'Texans', r'Colts', r'Titans', r'Jaguars',
    r'Commanders', r'Giants', r'Bengals', r'Browns', r'Red Sox', r'Yankees',
    r'Blue Jackets', r'Golden Knights', r'Penguins', r'Bruins', r'Rangers',
    r'Redhawks', r'Beavers', r'Raiders', r'Miners', r'Ole Miss', r'Miami vs',
    r'Middle Tennessee', r'UTEP', r'Seattle Redhawks', r'Oregon State',
    r'Tottenham', r'Champions League', r'Premier League', r'La Liga',
    r'HLTV', r'esports', r'Awper',
    # CRYPTO - todas las predicciones
    r'\bBitcoin\b', r'\bBTC\b', r'\bEthereum\b', r'\bETH\b',
    r'\bSolana\b', r'\bSOL\b', r'\bXRP\b', r'\bDogecoin\b', r'\bDOGE\b',
    r'\bCardano\b', r'\bADA\b', r'\bPolkadot\b', r'\bDOT\b',
    r'\bAvalanche\b', r'\bAVAX\b', r'\bChainlink\b', r'\bLINK\b',
    r'\bPolygon\b', r'\bMATIC\b', r'\bLitecoin\b', r'\bLTC\b',
    r'\bUniswap\b', r'\bUNI\b', r'\bShiba\b', r'\bSHIB\b',
    r'\bPepe\b', r'\bMeme coin\b', r'\bcrypto\b', r'\bcryptocurrency\b',
    r'Up or Down', r'updown', r'\d+PM.*ET', r'\d+AM.*ET',
    r'price of', r'above \$', r'below \$', r'close above', r'close below',
    r'all-time high', r'ATH', r'\bNFT\b', r'\bNFTs\b',
]

def is_insider_relevant(title: str) -> bool:
    """Verifica si el mercado es relevante para insider info (no deportes/crypto corto plazo)."""
    if not title:
        return False
    
    title_upper = title.upper()
    
    for pattern in EXCLUDE_KEYWORDS:
        if re.search(pattern, title, re.IGNORECASE):
            return False
    
    return True


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
    
    async def get_recent_trades(self, limit: int = 100, filter_insider: bool = True) -> list[Trade]:
        """Obtener trades recientes de toda la plataforma (endpoint público)."""
        try:
            # Pedimos más trades porque filtraremos muchos
            fetch_limit = limit * 3 if filter_insider else limit
            
            response = await self.client.get(
                f"{DATA_API_URL}/trades",
                params={"limit": fetch_limit}
            )
            response.raise_for_status()
            data = response.json()
            
            trades = []
            filtered_count = 0
            size_filtered = 0
            MIN_SIZE_USD = 5000  # Mínimo $5000 USD
            
            for item in data:
                try:
                    title = item.get("title", "")
                    size = float(item.get("size", 0))
                    
                    # Filtrar deportes y crypto
                    if filter_insider and not is_insider_relevant(title):
                        filtered_count += 1
                        continue
                    
                    # Filtrar trades menores a $5000
                    if size < MIN_SIZE_USD:
                        size_filtered += 1
                        continue
                    
                    trade = self._parse_trade(item)
                    if trade:
                        trades.append(trade)
                        
                    if len(trades) >= limit:
                        break
                        
                except Exception as e:
                    logger.warning("error_parseando_trade", error=str(e))
            
            print(f"Trades: {len(trades)} relevantes (>=$5k), {filtered_count} filtrados (categoria), {size_filtered} filtrados (<$5k)", flush=True)
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
                market_slug=item.get("eventSlug", item.get("slug", "")),
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
