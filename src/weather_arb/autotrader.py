"""Motor de autotrading para weather arb — ejecuta trades reales en Polymarket CLOB.

Flujo:
1. Recibe señales del WeatherArbDetector
2. Filtra por configuración del usuario (edge, confianza, ciudades, límites)
3. Coloca órdenes via py-clob-client
4. Registra trades en DB
5. Monitorea resultado y calcula PnL

Usa wallet SEPARADA del crypto arb (prefijo "wt_" en config DB).
"""
import asyncio
import time
from datetime import datetime, timezone
from typing import Optional
import structlog

from src import config

logger = structlog.get_logger()

# Constantes
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon
GAMMA_API = "https://gamma-api.polymarket.com"


class WeatherAutoTrader:
    """Ejecuta trades automáticos en Polymarket basados en señales weather arb.

    Wallet y credenciales SEPARADAS del crypto arb autotrader.
    Prefijo de config en DB: "wt_" (weather trader).
    """

    def __init__(self, db):
        self.db = db
        self._client = None  # py-clob-client ClobClient
        self._enabled = False
        self._config = {}
        self._last_trade_time = 0.0
        self._trades_today: list[dict] = []
        self._trades_today_date: str = ""
        self._open_positions: dict[str, dict] = {}  # condition_id → trade_info
        self._initialized = False
        self._user_id = 1
        self._processing_signals = False
        self._failed_cids: dict[str, float] = {}  # cid → timestamp blacklist

    async def initialize(self, user_id: int = None):
        """Cargar config y crear cliente CLOB si hay credenciales."""
        if user_id is not None:
            self._user_id = user_id
        try:
            raw = await self.db.get_config_bulk([
                "wt_enabled", "wt_bet_size", "wt_min_edge", "wt_min_confidence",
                "wt_max_odds", "wt_max_positions", "wt_order_type",
                "wt_max_daily_loss", "wt_max_daily_trades", "wt_cooldown_sec",
                "wt_cities",
                "wt_api_key", "wt_api_secret", "wt_private_key", "wt_passphrase",
                "wt_funder_address",
                # Bankroll
                "wt_bankroll", "wt_bet_mode", "wt_bet_pct",
                # Risk management
                "wt_stop_loss_enabled", "wt_stop_loss_pct",
                "wt_take_profit_pct", "wt_max_holding_sec",
            ], user_id=self._user_id)
            self._config = {
                "enabled": raw.get("wt_enabled") == "true",
                "bankroll": float(raw.get("wt_bankroll", 0)),
                "bet_mode": raw.get("wt_bet_mode", "fixed"),  # "fixed" o "proportional"
                "bet_size": float(raw.get("wt_bet_size", 1)),
                "bet_pct": float(raw.get("wt_bet_pct", 2)),
                "min_edge": float(raw.get("wt_min_edge", 8)),
                "min_confidence": float(raw.get("wt_min_confidence", 50)),
                "max_odds": float(raw.get("wt_max_odds", 0.85)),
                "max_positions": int(raw.get("wt_max_positions", 5)),
                "order_type": raw.get("wt_order_type", "market"),
                "max_daily_loss": float(raw.get("wt_max_daily_loss", 100)),
                "max_daily_trades": int(raw.get("wt_max_daily_trades", 20)),
                "cooldown_sec": int(raw.get("wt_cooldown_sec", 60)),
                "cities": [c.strip() for c in raw.get("wt_cities", "").split(",") if c.strip()] or None,
                # Wallet separada
                "api_key": raw.get("wt_api_key", ""),
                "api_secret": raw.get("wt_api_secret", ""),
                "private_key": raw.get("wt_private_key", ""),
                "passphrase": raw.get("wt_passphrase", ""),
                "funder_address": raw.get("wt_funder_address", ""),
                # Risk management
                "stop_loss_enabled": raw.get("wt_stop_loss_enabled") == "true",
                "stop_loss_pct": float(raw.get("wt_stop_loss_pct", 30)),
                "take_profit_pct": float(raw.get("wt_take_profit_pct", 50)),
                "max_holding_sec": int(raw.get("wt_max_holding_sec", 86400)),  # 24h default
            }
            self._enabled = self._config["enabled"]

            if self._enabled and self._config["api_key"] and self._config["private_key"]:
                self._init_clob_client()
            else:
                self._client = None

            await self._load_today_trades()
            await self._load_open_positions()

            self._initialized = True
            status = "ACTIVADO" if self._enabled and self._client else "DESACTIVADO"
            reason = ""
            if self._enabled and not self._client:
                reason = " (sin credenciales)"
            bankroll = self._config['bankroll']
            mode = self._config['bet_mode']
            sizing = f"${self._config['bet_size']}" if mode == "fixed" else f"{self._config['bet_pct']}%"
            print(f"[WeatherTrader] {status}{reason} | bankroll=${bankroll} "
                  f"modo={mode} sizing={sizing} "
                  f"edge>={self._config['min_edge']}% conf>={self._config['min_confidence']}%",
                  flush=True)
        except Exception as e:
            print(f"[WeatherTrader] Error inicializando: {e}", flush=True)
            self._enabled = False

    def _init_clob_client(self):
        """Crear cliente CLOB con credenciales de la wallet weather."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=self._config["api_key"],
                api_secret=self._config["api_secret"],
                api_passphrase=self._config["passphrase"],
            )
            pk = self._config["private_key"]
            if pk and not pk.startswith("0x"):
                pk = "0x" + pk
            funder = self._config.get("funder_address", "") or None
            self._client = ClobClient(
                CLOB_HOST,
                key=pk,
                chain_id=CHAIN_ID,
                signature_type=2,
                funder=funder,
                creds=creds,
            )
            print(f"[WeatherTrader] Cliente CLOB inicializado OK "
                  f"(funder={'set' if funder else 'NOT SET'})", flush=True)
        except ImportError:
            print("[WeatherTrader] ERROR: py-clob-client no instalado", flush=True)
            self._client = None
        except Exception as e:
            print(f"[WeatherTrader] Error creando cliente CLOB: {e}", flush=True)
            self._client = None

    async def reload_config(self, user_id: int = None):
        """Recargar config desde DB."""
        await self.initialize(user_id=user_id)

    def reset_state(self):
        """Resetear estado en memoria."""
        self._open_positions = {}
        self._trades_today = []
        self._trades_today_date = ""
        self._last_trade_time = 0.0
        self._failed_cids = {}
        print("[WeatherTrader] Estado reseteado", flush=True)

    async def _load_today_trades(self):
        """Cargar trades ejecutados hoy desde DB."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._trades_today_date != today:
            self._trades_today = []
            self._trades_today_date = today
        try:
            trades = await self.db.get_weather_trades(hours=24, user_id=self._user_id)
            self._trades_today = trades or []
        except Exception:
            self._trades_today = []

    async def _load_open_positions(self):
        """Cargar posiciones abiertas."""
        try:
            open_trades = await self.db.get_open_weather_trades(user_id=self._user_id)
            self._open_positions = {t["condition_id"]: t for t in (open_trades or [])}
        except Exception:
            self._open_positions = {}

    def _get_bankroll_available(self) -> float:
        """Calcular bankroll disponible = bankroll - en juego - pérdidas del día."""
        bankroll = self._config.get("bankroll", 0)
        if bankroll <= 0:
            return 0.0
        # Monto actualmente en posiciones abiertas
        in_play = sum(t.get("size_usd", 0) for t in self._open_positions.values())
        # PnL negativo del día (las pérdidas restan del bankroll)
        daily_pnl = sum(t.get("pnl", 0) for t in self._trades_today if t.get("resolved"))
        loss_offset = min(daily_pnl, 0)  # Solo pérdidas (negativo)
        available = bankroll - in_play + loss_offset
        return max(available, 0.0)

    # ── Evaluación de señales ──────────────────────────────────────────

    async def evaluate_signal(self, signal: dict) -> Optional[dict]:
        """Evaluar si una señal debe ejecutarse. Retorna trade_info o None."""
        city = signal.get("city_name", signal.get("city", "?"))
        tag = f"[WT] {city} {signal.get('range_label', '?')}"

        if not self._enabled or not self._client:
            return None

        cfg = self._config

        # Filtro: ciudad habilitada
        if cfg["cities"] and signal.get("city", "") not in cfg["cities"]:
            print(f"{tag} SKIP: ciudad no habilitada", flush=True)
            return None

        # Filtro: edge mínimo
        edge = signal.get("edge_pct", 0)
        if edge < cfg["min_edge"]:
            return None

        # Filtro: confianza mínima
        confidence = signal.get("confidence", 0)
        if confidence < cfg["min_confidence"]:
            return None

        # Filtro: odds máximo
        poly_odds = signal.get("poly_odds", 1.0)
        if poly_odds > cfg["max_odds"]:
            print(f"{tag} SKIP: odds {poly_odds} > max {cfg['max_odds']}", flush=True)
            return None

        # Filtro: cooldown
        now = time.time()
        if now - self._last_trade_time < cfg["cooldown_sec"]:
            return None

        # Filtro: max posiciones
        if len(self._open_positions) >= cfg["max_positions"]:
            print(f"{tag} SKIP: max positions ({len(self._open_positions)}>={cfg['max_positions']})", flush=True)
            return None

        # Filtro: max trades diarios
        if len(self._trades_today) >= cfg["max_daily_trades"]:
            return None

        # Filtro: max pérdida diaria
        daily_pnl = sum(t.get("pnl", 0) for t in self._trades_today if t.get("resolved"))
        if daily_pnl <= -cfg["max_daily_loss"]:
            return None

        # Filtro: no duplicar posición en mismo mercado
        cid = signal.get("condition_id", "")
        if cid in self._open_positions:
            return None

        # Filtro: no duplicar en mismo evento (diferente rango)
        event_slug = signal.get("event_slug", "")
        if event_slug:
            for pos in self._open_positions.values():
                if pos.get("event_slug") == event_slug:
                    print(f"{tag} SKIP: ya hay posición en {event_slug}", flush=True)
                    return None

        # Filtro: señal fallida recientemente
        if cid in self._failed_cids:
            if time.time() - self._failed_cids[cid] < 300:  # 5 min blacklist
                return None
            else:
                del self._failed_cids[cid]

        # ── Bankroll y sizing ──
        bankroll = cfg["bankroll"]
        if bankroll > 0:
            available = self._get_bankroll_available()
            if available <= 0:
                print(f"{tag} SKIP: bankroll agotado (${bankroll}, disponible=${available:.2f})", flush=True)
                return None
            # Calcular bet_size según modo
            if cfg["bet_mode"] == "proportional":
                bet_size = round(available * cfg["bet_pct"] / 100, 2)
            else:
                bet_size = cfg["bet_size"]
            # No apostar más de lo disponible
            if bet_size > available:
                bet_size = round(available, 2)
            # Mínimo viable ($0.50)
            if bet_size < 0.50:
                print(f"{tag} SKIP: bet ${bet_size:.2f} < mínimo $0.50", flush=True)
                return None
        else:
            # Sin bankroll configurado: usar bet_size fijo directo
            if cfg["bet_mode"] == "proportional":
                print(f"{tag} SKIP: modo proporcional requiere bankroll > 0", flush=True)
                return None
            bet_size = cfg["bet_size"]

        print(f"{tag} PASS: edge={edge:.1f}% conf={confidence:.0f}% odds={poly_odds:.2f} -> EXECUTING ${bet_size:.2f}",
              flush=True)

        return {
            "condition_id": cid,
            "city": signal.get("city", ""),
            "city_name": signal.get("city_name", ""),
            "date": signal.get("date", ""),
            "range_label": signal.get("range_label", ""),
            "edge_pct": edge,
            "confidence": confidence,
            "poly_odds": poly_odds,
            "ensemble_prob": signal.get("ensemble_prob", 0),
            "bet_size": bet_size,
            "order_type": cfg["order_type"],
            "event_slug": event_slug,
            "market_question": signal.get("market_question", ""),
            "token_id": signal.get("token_id", ""),
            "unit": signal.get("unit", ""),
        }

    # ── Ejecución de órdenes ──────────────────────────────────────────

    async def execute_trade(self, trade_info: dict) -> dict:
        """Ejecutar un trade en Polymarket CLOB."""
        if not self._client:
            return {"success": False, "error": "Cliente CLOB no inicializado"}

        cid = trade_info["condition_id"]
        bet_size = trade_info["bet_size"]
        price = trade_info["poly_odds"]
        token_id = trade_info.get("token_id", "")

        try:
            # Si no tenemos token_id del detector, obtenerlo del CLOB
            if not token_id:
                token_id = await self._get_yes_token_id(cid)
                if not token_id:
                    return {"success": False, "error": "No se encontró token_id Yes"}

            # Precio con slippage para FOK
            is_fok = trade_info["order_type"] == "market"
            if is_fok:
                order_price = min(round(price + 0.03, 2), 0.99)
            else:
                order_price = round(price, 2)

            if order_price <= 0 or order_price >= 1:
                return {"success": False, "error": f"Precio inválido: {order_price}"}

            # Calcular shares
            from decimal import Decimal, ROUND_DOWN
            MIN_CLOB_SHARES = Decimal('5')
            d_price = Decimal(str(order_price))
            d_raw = Decimal(str(bet_size)) / d_price

            d_shares = Decimal('0')
            for decimals in [4, 3, 2, 1, 0]:
                q = Decimal(10) ** -decimals
                d_candidate = d_raw.quantize(q, rounding=ROUND_DOWN)
                d_maker = d_candidate * d_price
                if d_maker == d_maker.quantize(Decimal('0.01')):
                    d_shares = d_candidate
                    break

            if d_shares < MIN_CLOB_SHARES:
                d_shares = MIN_CLOB_SHARES
                bet_size = float((d_shares * d_price).quantize(Decimal('0.01')))

            shares = float(d_shares)
            if shares <= 0:
                self._failed_cids[cid] = time.time()
                return {"success": False, "error": "Shares inválidas"}

            print(f"[WeatherTrader] Order: price={order_price} shares={shares} "
                  f"usdc={round(shares*order_price,2)} type={trade_info['order_type']}", flush=True)

            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                price=order_price,
                size=shares,
                side="BUY",
                token_id=token_id,
            )

            order_type = OrderType.FOK if is_fok else OrderType.GTC
            loop = asyncio.get_running_loop()
            signed_order = await loop.run_in_executor(None, self._client.create_order, order_args)
            resp = await loop.run_in_executor(None, self._client.post_order, signed_order, order_type)

            # Parsear respuesta
            success = False
            order_id = ""
            error_msg = ""

            if isinstance(resp, dict):
                order_id = resp.get("orderID", resp.get("order_id", "")) or ""
                success = bool(order_id) and resp.get("success", True)
                if not success:
                    error_msg = resp.get("errorMsg", resp.get("error", str(resp)))
            elif hasattr(resp, "success"):
                success = resp.success
                order_id = getattr(resp, "orderID", "") or ""
                error_msg = getattr(resp, "errorMsg", "")
            else:
                order_id = str(resp) if resp else ""
                success = bool(order_id)

            # GTC live: polling para verificar fill
            resp_status = resp.get("status", "") if isinstance(resp, dict) else getattr(resp, "status", "")
            if success and str(resp_status).lower() == "live" and order_id:
                print(f"[WeatherTrader] ⏳ Orden GTC en orderbook, esperando fill...", flush=True)
                filled = False
                for attempt in range(10):  # 10 × 5s = 50s máximo
                    await asyncio.sleep(5)
                    try:
                        order_info = await loop.run_in_executor(None, self._client.get_order, order_id)
                        current_status = ""
                        if isinstance(order_info, dict):
                            current_status = order_info.get("status", "")
                        elif hasattr(order_info, "status"):
                            current_status = getattr(order_info, "status", "")
                        if current_status.lower() == "matched":
                            filled = True
                            break
                        elif current_status.lower() in ("cancelled", "expired", ""):
                            break
                    except Exception:
                        break
                if not filled:
                    try:
                        await loop.run_in_executor(None, self._client.cancel, order_id)
                    except Exception:
                        pass
                    success = False
                    error_msg = "GTC not filled, cancelled"

            if success:
                self._last_trade_time = time.time()
                trade_record = {
                    "condition_id": cid,
                    "order_id": order_id,
                    "city": trade_info["city"],
                    "city_name": trade_info["city_name"],
                    "date": trade_info["date"],
                    "range_label": trade_info["range_label"],
                    "side": "BUY",
                    "price": order_price,
                    "size_usd": bet_size,
                    "shares": shares,
                    "token_id": token_id,
                    "edge_pct": trade_info["edge_pct"],
                    "confidence": trade_info["confidence"],
                    "ensemble_prob": trade_info["ensemble_prob"],
                    "event_slug": trade_info["event_slug"],
                    "order_type": trade_info["order_type"],
                    "unit": trade_info.get("unit", ""),
                    "status": "filled",
                    "created_ts": time.time(),
                }
                await self.db.record_weather_trade(trade_record, user_id=self._user_id)
                self._trades_today.append(trade_record)
                self._open_positions[cid] = trade_record
                print(f"[WeatherTrader] ✅ TRADE: {trade_info['city_name']} {trade_info['range_label']} "
                      f"${bet_size} @ {price:.2f} (edge={trade_info['edge_pct']:.1f}%) "
                      f"order={order_id}", flush=True)
                return {"success": True, "order_id": order_id, "trade": trade_record}
            else:
                print(f"[WeatherTrader] ❌ Orden rechazada: {error_msg}", flush=True)
                self._failed_cids[cid] = time.time()
                return {"success": False, "error": error_msg}

        except Exception as e:
            print(f"[WeatherTrader] ❌ Error: {e}", flush=True)
            self._failed_cids[cid] = time.time()
            return {"success": False, "error": str(e)}

    async def _get_yes_token_id(self, condition_id: str) -> Optional[str]:
        """Obtener token_id del outcome 'Yes' via CLOB API."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{CLOB_HOST}/markets/{condition_id}")
                if resp.status_code != 200:
                    return None
                data = resp.json()
                tokens = data.get("tokens", [])
                for tok in tokens:
                    if tok.get("outcome", "").lower() == "yes":
                        return tok.get("token_id", "")
        except Exception as e:
            print(f"[WeatherTrader] Error obteniendo token_id: {e}", flush=True)
        return None

    # ── Proceso de señales ─────────────────────────────────────────────

    async def process_signals(self, signals: list[dict]):
        """Evaluar y ejecutar señales. Llamado desde loop principal."""
        if not self._enabled or not self._client or not self._initialized:
            return
        if self._processing_signals:
            return
        self._processing_signals = True
        try:
            # Recargar trades si cambió el día
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if self._trades_today_date != today:
                await self._load_today_trades()

            for signal in signals:
                trade_info = await self.evaluate_signal(signal)
                if trade_info:
                    result = await self.execute_trade(trade_info)
                    if result.get("success"):
                        await asyncio.sleep(2)
        finally:
            self._processing_signals = False

    # ── Resolución de trades ──────────────────────────────────────────

    async def resolve_trades(self):
        """Resolver trades abiertos (mercados weather cerrados)."""
        if not self._open_positions:
            return

        try:
            import httpx
            resolved = 0

            async with httpx.AsyncClient(timeout=10) as client:
                for cid, trade in list(self._open_positions.items()):
                    try:
                        # Verificar si mercado cerró via CLOB
                        resp = await client.get(f"{CLOB_HOST}/markets/{cid}")
                        if resp.status_code != 200:
                            continue
                        data = resp.json()
                        if not data.get("closed"):
                            continue

                        # Buscar outcome ganador
                        winning_outcome = None
                        tokens = data.get("tokens", [])
                        for tok in tokens:
                            if tok.get("winner") is True:
                                winning_outcome = tok.get("outcome", "")
                                break
                            try:
                                if float(tok.get("price", 0) or 0) >= 0.95:
                                    winning_outcome = tok.get("outcome", "")
                                    break
                            except (ValueError, TypeError):
                                pass

                        if not winning_outcome:
                            # Fallback: Gamma API
                            event_slug = trade.get("event_slug", "")
                            if event_slug:
                                try:
                                    resp2 = await client.get(
                                        f"{GAMMA_API}/events", params={"slug": event_slug})
                                    if resp2.status_code == 200:
                                        events = resp2.json()
                                        if events and events[0].get("closed"):
                                            for m in events[0].get("markets", []):
                                                if m.get("conditionId") == cid:
                                                    op = m.get("outcomePrices")
                                                    if op:
                                                        import json as _json
                                                        prices = _json.loads(op) if isinstance(op, str) else op
                                                        if float(prices[0]) >= 0.90:
                                                            winning_outcome = "Yes"
                                                        elif float(prices[1]) >= 0.90:
                                                            winning_outcome = "No"
                                except Exception:
                                    pass

                        if not winning_outcome:
                            continue

                        # PnL: compramos Yes
                        won = winning_outcome.lower() == "yes"
                        shares = trade.get("shares", 0)
                        size_usd = trade.get("size_usd", 0)

                        if won:
                            pnl = round(shares - size_usd, 2) if shares > 0 else 0
                            result = "win"
                        else:
                            pnl = -size_usd
                            result = "loss"

                        await self.db.resolve_weather_trade(cid, result, pnl, user_id=self._user_id)
                        trade["resolved"] = True
                        trade["result"] = result
                        trade["pnl"] = pnl
                        del self._open_positions[cid]
                        resolved += 1

                        emoji = "✅" if result == "win" else "❌"
                        print(f"[WeatherTrader] {emoji} {trade.get('city_name','')} "
                              f"{trade.get('range_label','')} → {result.upper()} PnL=${pnl:.2f}",
                              flush=True)

                    except Exception as e:
                        print(f"[WeatherTrader] Error resolviendo {cid}: {e}", flush=True)

            if resolved:
                await self._load_today_trades()

        except Exception as e:
            print(f"[WeatherTrader] Error en resolve_trades: {e}", flush=True)

    # ── Consultas de estado ───────────────────────────────────────────

    def get_status(self) -> dict:
        """Estado actual para el dashboard."""
        daily_pnl = sum(t.get("pnl", 0) for t in self._trades_today if t.get("resolved"))
        in_play = sum(t.get("size_usd", 0) for t in self._open_positions.values())
        bankroll = self._config.get("bankroll", 0)
        available = self._get_bankroll_available() if bankroll > 0 else 0
        return {
            "enabled": self._enabled,
            "connected": self._client is not None,
            "open_positions": len(self._open_positions),
            "trades_today": len(self._trades_today),
            "pnl_today": round(daily_pnl, 2),
            "bankroll": bankroll,
            "bankroll_available": round(available, 2),
            "bankroll_in_play": round(in_play, 2),
            "bet_mode": self._config.get("bet_mode", "fixed"),
        }

    async def get_usdc_balance(self, wallet_address: str = "") -> Optional[float]:
        """Consultar saldo USDC de la wallet weather."""
        if not wallet_address:
            pk = self._config.get("private_key", "")
            if not pk:
                return None
            try:
                from eth_account import Account
                if not pk.startswith("0x"):
                    pk = "0x" + pk
                wallet_address = Account.from_key(pk).address
            except Exception:
                return None

        import httpx
        USDC_CONTRACTS = [
            "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
            "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        ]
        total = 0.0
        addr_padded = wallet_address.lower().replace("0x", "").zfill(64)

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for contract in USDC_CONTRACTS:
                    payload = {
                        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                        "params": [{"to": contract,
                                    "data": f"0x70a08231000000000000000000000000{addr_padded}"},
                                   "latest"]
                    }
                    resp = await client.post("https://polygon-rpc.com", json=payload)
                    if resp.status_code == 200:
                        result = resp.json().get("result", "0x0")
                        if result and result != "0x":
                            total += int(result, 16) / 1e6
        except Exception:
            pass

        return round(total, 2) if total > 0 else None

    async def test_connection(self) -> dict:
        """Probar conexión al CLOB."""
        if not self._config.get("api_key"):
            return {"connected": False, "error": "Sin credenciales configuradas"}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{CLOB_HOST}/time")
                if resp.status_code != 200:
                    return {"connected": False, "error": f"CLOB status {resp.status_code}"}
            if not self._client:
                self._init_clob_client()
            if not self._client:
                return {"connected": False, "error": "No se pudo crear cliente CLOB"}
            balance = await self.get_usdc_balance()
            return {"connected": True, "balance": balance, "note": "Conexión OK"}
        except Exception as e:
            return {"connected": False, "error": str(e)}
