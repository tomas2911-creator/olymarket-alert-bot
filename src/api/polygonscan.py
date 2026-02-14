"""Cliente Polygonscan para análisis on-chain de wallets."""
import httpx
from datetime import datetime
from typing import Optional
import structlog

from src import config

logger = structlog.get_logger()

POLYGONSCAN_API = "https://api.polygonscan.com/api"
# USDC contracts on Polygon
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
USDC_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


async def get_wallet_onchain_info(address: str) -> dict:
    """Obtener info on-chain de una wallet: primera TX y fuente de fondeo."""
    result = {"first_tx": None, "funded_by": None}
    api_key = config.POLYGONSCAN_API_KEY
    if not api_key:
        return result

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # 1. Primera transacción (edad de la wallet)
            resp = await client.get(POLYGONSCAN_API, params={
                "module": "account", "action": "txlist",
                "address": address, "startblock": "0", "endblock": "99999999",
                "page": "1", "offset": "1", "sort": "asc",
                "apikey": api_key,
            })
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "1" and data.get("result"):
                    tx = data["result"][0]
                    ts = int(tx.get("timeStamp", 0))
                    if ts > 0:
                        result["first_tx"] = datetime.fromtimestamp(ts)

            # 2. Primeras transferencias USDC entrantes (fuente de fondeo)
            for usdc_addr in [USDC_NATIVE, USDC_BRIDGED]:
                resp2 = await client.get(POLYGONSCAN_API, params={
                    "module": "account", "action": "tokentx",
                    "address": address, "contractaddress": usdc_addr,
                    "page": "1", "offset": "3", "sort": "asc",
                    "apikey": api_key,
                })
                if resp2.status_code == 200:
                    data2 = resp2.json()
                    if data2.get("status") == "1" and data2.get("result"):
                        for tx in data2["result"]:
                            if tx.get("to", "").lower() == address.lower():
                                result["funded_by"] = tx.get("from", "")
                                break
                if result["funded_by"]:
                    break

    except Exception as e:
        logger.warning("polygonscan_error", address=address[:10], error=str(e))

    return result
