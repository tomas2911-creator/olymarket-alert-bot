"""Cliente Etherscan V2 para análisis on-chain de wallets en Polygon.

Usa Etherscan V2 API (api.etherscan.io/v2/api con chainid=137):
1. Normal Transactions (txlist) → edad on-chain, cantidad de TXs
2. ERC20 Token Transfers (tokentx) → flujo de USDC (in/out), fuente de fondeo
3. ERC1155 Token Transfers (token1155tx) → posiciones en Polymarket (shares)
"""
import httpx
from datetime import datetime
from typing import Optional
import structlog
import asyncio

from src import config

logger = structlog.get_logger()

# Etherscan V2 API — soporta multi-chain con chainid
ETHERSCAN_V2_API = "https://api.etherscan.io/v2/api"
CHAINID_POLYGON = "137"

# USDC en Polygon
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"   # USDC nativo
USDC_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"   # USDC.e bridged
USDC_DECIMALS = {USDC_NATIVE.lower(): 6, USDC_BRIDGED.lower(): 6}

# Polymarket CTF (Conditional Token Framework) — ERC1155
POLYMARKET_CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"


async def _api_call(client: httpx.AsyncClient, params: dict) -> dict:
    """Llamada genérica a Etherscan V2 API con chainid y manejo de errores."""
    params["chainid"] = CHAINID_POLYGON
    params["apikey"] = config.POLYGONSCAN_API_KEY
    try:
        resp = await client.get(ETHERSCAN_V2_API, params=params)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "1" and isinstance(data.get("result"), list):
                return data
            else:
                logger.warning("polygonscan_api_no_data",
                             status=data.get("status"),
                             message=data.get("message", ""),
                             result=str(data.get("result", ""))[:120],
                             action=params.get("action", ""))
        else:
            logger.warning("polygonscan_api_http_error", status_code=resp.status_code,
                           body=resp.text[:200])
    except Exception as e:
        logger.warning("polygonscan_api_exception", error=str(e))
    return {"status": "0", "result": []}


async def get_wallet_onchain_info(address: str) -> dict:
    """Análisis on-chain completo de una wallet en Polygon.

    Retorna dict con:
    - first_tx: datetime de la primera transacción
    - funded_by: dirección que envió el primer USDC
    - age_days: edad de la wallet en días
    - tx_count: cantidad de transacciones normales (últimas 1000)
    - usdc_in: total USDC recibido
    - usdc_out: total USDC enviado
    - erc1155_transfers: cantidad de transfers de posiciones (shares)
    """
    result = {
        "first_tx": None, "funded_by": None, "age_days": None,
        "tx_count": None, "usdc_in": None, "usdc_out": None,
        "erc1155_transfers": None,
    }
    api_key = config.POLYGONSCAN_API_KEY
    if not api_key:
        return result

    addr_lower = address.lower()

    try:
        async with httpx.AsyncClient(timeout=20) as client:

            # ═══════════════════════════════════════════════════════
            # 1) NORMAL TRANSACTIONS → edad + actividad
            # ═══════════════════════════════════════════════════════
            data = await _api_call(client, {
                "module": "account", "action": "txlist",
                "address": address, "startblock": "0", "endblock": "99999999",
                "page": "1", "offset": "1000", "sort": "asc",
            })
            txs = data.get("result", [])
            if txs:
                # Primera TX = edad
                ts = int(txs[0].get("timeStamp", 0))
                if ts > 0:
                    first_dt = datetime.fromtimestamp(ts)
                    result["first_tx"] = first_dt
                    result["age_days"] = (datetime.now() - first_dt).days
                result["tx_count"] = len(txs)

            await asyncio.sleep(0.25)  # Rate limit

            # ═══════════════════════════════════════════════════════
            # 2) ERC20 TRANSFERS → flujo USDC + fuente de fondeo
            # ═══════════════════════════════════════════════════════
            usdc_in = 0.0
            usdc_out = 0.0
            funded_by = None

            for usdc_addr in [USDC_NATIVE, USDC_BRIDGED]:
                data2 = await _api_call(client, {
                    "module": "account", "action": "tokentx",
                    "address": address, "contractaddress": usdc_addr,
                    "page": "1", "offset": "200", "sort": "asc",
                })
                transfers = data2.get("result", [])
                decimals = USDC_DECIMALS.get(usdc_addr.lower(), 6)

                for tx in transfers:
                    value_raw = int(tx.get("value", "0") or "0")
                    value = value_raw / (10 ** decimals)

                    to_addr = (tx.get("to") or "").lower()
                    from_addr = (tx.get("from") or "").lower()

                    if to_addr == addr_lower:
                        # USDC entrante
                        usdc_in += value
                        # Primer envío de USDC = funded_by
                        if funded_by is None and value > 0:
                            funded_by = tx.get("from", "")
                    elif from_addr == addr_lower:
                        # USDC saliente
                        usdc_out += value

                await asyncio.sleep(0.25)  # Rate limit

            result["usdc_in"] = round(usdc_in, 2) if usdc_in > 0 else None
            result["usdc_out"] = round(usdc_out, 2) if usdc_out > 0 else None
            result["funded_by"] = funded_by

            # ═══════════════════════════════════════════════════════
            # 3) ERC1155 TRANSFERS → posiciones Polymarket (shares)
            # ═══════════════════════════════════════════════════════
            data3 = await _api_call(client, {
                "module": "account", "action": "token1155tx",
                "address": address, "contractaddress": POLYMARKET_CTF,
                "page": "1", "offset": "500", "sort": "desc",
            })
            erc1155_txs = data3.get("result", [])
            result["erc1155_transfers"] = len(erc1155_txs) if erc1155_txs else None

            logger.info("polygonscan_ok",
                        address=address[:10],
                        age_days=result["age_days"],
                        tx_count=result["tx_count"],
                        usdc_in=result["usdc_in"],
                        erc1155=result["erc1155_transfers"])

    except Exception as e:
        logger.warning("polygonscan_error", address=address[:10], error=str(e))

    return result


async def get_usdc_capital(address: str) -> dict:
    """Versión ligera: solo USDC in/out + funded_by (2 API calls).
    Optimizada para batch scan — no consulta txlist ni erc1155."""
    result = {"usdc_in": None, "usdc_out": None, "funded_by": None}
    if not config.POLYGONSCAN_API_KEY:
        return result

    addr_lower = address.lower()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            usdc_in = 0.0
            usdc_out = 0.0
            funded_by = None

            for usdc_addr in [USDC_NATIVE, USDC_BRIDGED]:
                data = await _api_call(client, {
                    "module": "account", "action": "tokentx",
                    "address": address, "contractaddress": usdc_addr,
                    "page": "1", "offset": "200", "sort": "asc",
                })
                transfers = data.get("result", [])
                decimals = USDC_DECIMALS.get(usdc_addr.lower(), 6)

                for tx in transfers:
                    value_raw = int(tx.get("value", "0") or "0")
                    value = value_raw / (10 ** decimals)
                    to_addr = (tx.get("to") or "").lower()
                    from_addr = (tx.get("from") or "").lower()

                    if to_addr == addr_lower:
                        usdc_in += value
                        if funded_by is None and value > 0:
                            funded_by = tx.get("from", "")
                    elif from_addr == addr_lower:
                        usdc_out += value

                await asyncio.sleep(0.22)

            result["usdc_in"] = round(usdc_in, 2) if usdc_in > 0 else None
            result["usdc_out"] = round(usdc_out, 2) if usdc_out > 0 else None
            result["funded_by"] = funded_by
            logger.info("polygonscan_capital_result", address=address[:10],
                        usdc_in=result["usdc_in"], usdc_out=result["usdc_out"])

    except Exception as e:
        logger.warning("polygonscan_capital_error", address=address[:10], error=str(e))

    return result
