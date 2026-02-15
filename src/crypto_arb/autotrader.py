"""Motor de autotrading para crypto arb — ejecuta trades reales en Polymarket CLOB.

Flujo:
1. Recibe señales del CryptoArbDetector
2. Filtra por configuración del usuario (edge, confianza, monedas, límites)
3. Coloca órdenes via py-clob-client
4. Registra trades en DB
5. Monitorea resultado
"""
import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog

logger = structlog.get_logger()

# Constantes
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon


class AutoTrader:
    """Ejecuta trades automáticos en Polymarket basados en señales crypto arb."""

    def __init__(self, db):
        self.db = db
        self._client = None  # py-clob-client ClobClient
        self._enabled = False
        self._config = {}
        self._last_trade_time = 0.0
        self._trades_today: list[dict] = []
        self._trades_today_date: str = ""
        self._open_positions: dict[str, dict] = {}  # condition_id -> trade_info
        self._initialized = False

    async def initialize(self):
        """Cargar config y crear cliente CLOB si hay credenciales."""
        try:
            raw = await self.db.get_config_bulk([
                "at_enabled", "at_bet_size", "at_min_edge", "at_min_confidence",
                "at_max_odds", "at_max_positions", "at_order_type",
                "at_max_daily_loss", "at_max_daily_trades", "at_cooldown_sec",
                "at_coins", "at_api_key", "at_api_secret", "at_private_key", "at_passphrase",
            ])
            self._config = {
                "enabled": raw.get("at_enabled") == "true",
                "bet_size": float(raw.get("at_bet_size", 5)),
                "min_edge": float(raw.get("at_min_edge", 15)),
                "min_confidence": float(raw.get("at_min_confidence", 75)),
                "max_odds": float(raw.get("at_max_odds", 0.55)),
                "max_positions": int(raw.get("at_max_positions", 3)),
                "order_type": raw.get("at_order_type", "limit"),
                "max_daily_loss": float(raw.get("at_max_daily_loss", 50)),
                "max_daily_trades": int(raw.get("at_max_daily_trades", 20)),
                "cooldown_sec": int(raw.get("at_cooldown_sec", 30)),
                "coins": raw.get("at_coins", "BTC,ETH,SOL").split(","),
                "api_key": raw.get("at_api_key", ""),
                "api_secret": raw.get("at_api_secret", ""),
                "private_key": raw.get("at_private_key", ""),
                "passphrase": raw.get("at_passphrase", ""),
            }
            self._enabled = self._config["enabled"]

            if self._enabled and self._config["api_key"] and self._config["private_key"]:
                self._init_clob_client()
            else:
                self._client = None

            # Cargar trades de hoy desde DB
            await self._load_today_trades()
            # Cargar posiciones abiertas
            await self._load_open_positions()

            self._initialized = True
            status = "ACTIVADO" if self._enabled and self._client else "DESACTIVADO"
            reason = ""
            if self._enabled and not self._client:
                reason = " (sin credenciales)"
            print(f"[AutoTrader] {status}{reason} | bet=${self._config['bet_size']} "
                  f"edge>={self._config['min_edge']}% conf>={self._config['min_confidence']}%",
                  flush=True)
        except Exception as e:
            print(f"[AutoTrader] Error inicializando: {e}", flush=True)
            self._enabled = False

    def _init_clob_client(self):
        """Crear cliente CLOB con credenciales."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=self._config["api_key"],
                api_secret=self._config["api_secret"],
                api_passphrase=self._config["passphrase"],
            )
            self._client = ClobClient(
                CLOB_HOST,
                key=self._config["private_key"],
                chain_id=CHAIN_ID,
                signature_type=2,
                creds=creds,
            )
            print("[AutoTrader] Cliente CLOB inicializado OK", flush=True)
        except ImportError:
            print("[AutoTrader] ERROR: py-clob-client no instalado. pip install py-clob-client", flush=True)
            self._client = None
        except Exception as e:
            print(f"[AutoTrader] Error creando cliente CLOB: {e}", flush=True)
            self._client = None

    async def reload_config(self):
        """Recargar config desde DB (llamado cuando se guarda config desde dashboard)."""
        await self.initialize()

    async def _load_today_trades(self):
        """Cargar trades ejecutados hoy desde DB."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._trades_today_date != today:
            self._trades_today = []
            self._trades_today_date = today
        try:
            trades = await self.db.get_autotrades(hours=24)
            self._trades_today = trades or []
        except Exception:
            self._trades_today = []

    async def _load_open_positions(self):
        """Cargar posiciones abiertas (trades sin resolver)."""
        try:
            open_trades = await self.db.get_open_autotrades()
            self._open_positions = {t["condition_id"]: t for t in (open_trades or [])}
        except Exception:
            self._open_positions = {}

    # ── Evaluación de señales ──────────────────────────────────────────

    async def evaluate_signal(self, signal: dict) -> Optional[dict]:
        """Evaluar si una señal debe ejecutarse. Retorna trade_info o None."""
        if not self._enabled or not self._client:
            return None

        cfg = self._config

        # Filtro: moneda habilitada
        if signal.get("coin", "") not in cfg["coins"]:
            return None

        # Filtro: edge mínimo
        edge = signal.get("edge_pct", 0)
        if edge < cfg["min_edge"]:
            return None

        # Filtro: confianza mínima
        confidence = signal.get("confidence", 0)
        if confidence < cfg["min_confidence"]:
            return None

        # Filtro: odds máximo (no comprar si el precio ya es alto)
        poly_odds = signal.get("poly_odds", 1.0)
        if poly_odds > cfg["max_odds"]:
            return None

        # Filtro: cooldown entre trades
        now = time.time()
        if now - self._last_trade_time < cfg["cooldown_sec"]:
            return None

        # Filtro: max posiciones abiertas
        if len(self._open_positions) >= cfg["max_positions"]:
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

        # Filtro: tiempo restante mínimo (no entrar si queda muy poco)
        remaining = signal.get("time_remaining_sec", 0)
        if remaining < 30:
            return None

        return {
            "condition_id": cid,
            "coin": signal["coin"],
            "direction": signal["direction"],
            "edge_pct": edge,
            "confidence": confidence,
            "poly_odds": poly_odds,
            "fair_odds": signal.get("fair_odds", 0),
            "bet_size": cfg["bet_size"],
            "order_type": cfg["order_type"],
            "spot_price": signal.get("spot_price", 0),
            "event_slug": signal.get("event_slug", ""),
            "market_question": signal.get("market_question", ""),
            "time_remaining_sec": remaining,
        }

    # ── Ejecución de órdenes ──────────────────────────────────────────

    async def execute_trade(self, trade_info: dict) -> dict:
        """Ejecutar un trade en Polymarket CLOB.
        Retorna dict con resultado de la orden.
        """
        if not self._client:
            return {"success": False, "error": "Cliente CLOB no inicializado"}

        cid = trade_info["condition_id"]
        direction = trade_info["direction"]
        bet_size = trade_info["bet_size"]
        price = trade_info["poly_odds"]

        try:
            # Obtener token_id del outcome correcto
            token_id = await self._get_token_id(cid, direction)
            if not token_id:
                return {"success": False, "error": f"No se encontró token_id para {direction}"}

            # Calcular cantidad de shares: size / price
            shares = round(bet_size / price, 2)

            # Crear y enviar orden
            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                price=price,
                size=shares,
                side="BUY",
                token_id=token_id,
            )

            order_type = OrderType.FOK if trade_info["order_type"] == "market" else OrderType.GTC
            # py-clob-client es síncrono — ejecutar en executor para no bloquear event loop
            loop = asyncio.get_event_loop()
            signed_order = await loop.run_in_executor(None, self._client.create_order, order_args)
            resp = await loop.run_in_executor(None, self._client.post_order, signed_order, order_type)

            # Parsear respuesta
            success = False
            order_id = ""
            error_msg = ""

            if isinstance(resp, dict):
                success = resp.get("success", False) or resp.get("orderID") is not None
                order_id = resp.get("orderID", resp.get("order_id", ""))
                if not success:
                    error_msg = resp.get("errorMsg", resp.get("error", str(resp)))
            elif hasattr(resp, "success"):
                success = resp.success
                order_id = getattr(resp, "orderID", "")
                error_msg = getattr(resp, "errorMsg", "")
            else:
                # Respuesta inesperada
                success = bool(resp)
                order_id = str(resp) if resp else ""

            if success:
                self._last_trade_time = time.time()
                trade_record = {
                    "condition_id": cid,
                    "order_id": order_id,
                    "coin": trade_info["coin"],
                    "direction": direction,
                    "side": "BUY",
                    "price": price,
                    "size_usd": bet_size,
                    "shares": shares,
                    "token_id": token_id,
                    "edge_pct": trade_info["edge_pct"],
                    "confidence": trade_info["confidence"],
                    "event_slug": trade_info["event_slug"],
                    "order_type": trade_info["order_type"],
                    "status": "filled",
                }
                # Guardar en DB
                await self.db.record_autotrade(trade_record)
                # Actualizar estado local
                self._trades_today.append(trade_record)
                self._open_positions[cid] = trade_record
                print(f"[AutoTrader] ✅ TRADE EJECUTADO: {trade_info['coin']} {direction.upper()} "
                      f"${bet_size} @ {price:.2f} (edge={trade_info['edge_pct']:.1f}% "
                      f"conf={trade_info['confidence']:.0f}%) order={order_id}",
                      flush=True)
                return {"success": True, "order_id": order_id, "trade": trade_record}
            else:
                print(f"[AutoTrader] ❌ Orden rechazada: {error_msg}", flush=True)
                # Guardar intento fallido
                await self.db.record_autotrade({
                    "condition_id": cid,
                    "order_id": "",
                    "coin": trade_info["coin"],
                    "direction": direction,
                    "side": "BUY",
                    "price": price,
                    "size_usd": bet_size,
                    "shares": shares,
                    "token_id": token_id,
                    "edge_pct": trade_info["edge_pct"],
                    "confidence": trade_info["confidence"],
                    "event_slug": trade_info["event_slug"],
                    "order_type": trade_info["order_type"],
                    "status": "rejected",
                    "error": error_msg,
                })
                return {"success": False, "error": error_msg}

        except Exception as e:
            error = str(e)
            print(f"[AutoTrader] ❌ Error ejecutando trade: {error}", flush=True)
            return {"success": False, "error": error}

    async def _get_token_id(self, condition_id: str, direction: str) -> Optional[str]:
        """Obtener token_id del outcome correcto (Up/Down) via CLOB API."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{CLOB_HOST}/markets/{condition_id}")
                if resp.status_code != 200:
                    return None
                data = resp.json()
                tokens = data.get("tokens", [])
                # Buscar token que matchee la dirección
                target = "Up" if direction == "up" else "Down"
                for token in tokens:
                    outcome = token.get("outcome", "")
                    if outcome.lower() == target.lower() or \
                       (direction == "up" and outcome == "Yes") or \
                       (direction == "down" and outcome == "No"):
                        return token.get("token_id", "")
                # Fallback: Yes=Up, No=Down (convención estándar)
                if tokens:
                    if direction == "up":
                        return tokens[0].get("token_id", "")
                    elif len(tokens) > 1:
                        return tokens[1].get("token_id", "")
        except Exception as e:
            print(f"[AutoTrader] Error obteniendo token_id: {e}", flush=True)
        return None

    # ── Proceso de señales (llamado desde main.py) ────────────────────

    async def process_signals(self, signals: list[dict]):
        """Evaluar y ejecutar señales. Llamado periódicamente desde main loop."""
        if not self._enabled or not self._client or not self._initialized:
            return

        # Recargar trades de hoy si cambió el día
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._trades_today_date != today:
            await self._load_today_trades()

        for signal in signals:
            trade_info = await self.evaluate_signal(signal)
            if trade_info:
                result = await self.execute_trade(trade_info)
                if result.get("success"):
                    # Pequeña pausa entre trades para no saturar
                    await asyncio.sleep(2)

    # ── Resolución de trades ──────────────────────────────────────────

    async def resolve_trades(self):
        """Resolver trades abiertos y calcular PnL real."""
        if not self._open_positions:
            return

        try:
            import httpx
            now = datetime.now(timezone.utc)
            resolved = 0

            async with httpx.AsyncClient(timeout=10) as client:
                for cid, trade in list(self._open_positions.items()):
                    try:
                        resp = await client.get(f"{CLOB_HOST}/markets/{cid}")
                        if resp.status_code != 200:
                            continue
                        data = resp.json()
                        if not data.get("closed"):
                            continue

                        # Mercado cerrado — determinar resultado
                        tokens = data.get("tokens", [])
                        winning_outcome = None
                        for token in tokens:
                            if token.get("winner") is True:
                                winning_outcome = token.get("outcome", "")
                                break
                            if float(token.get("price", 0)) >= 0.95:
                                winning_outcome = token.get("outcome", "")
                                break

                        if not winning_outcome:
                            continue

                        # Determinar si ganamos
                        direction = trade.get("direction", "")
                        won = (direction == "up" and winning_outcome.lower() in ("up", "yes")) or \
                              (direction == "down" and winning_outcome.lower() in ("down", "no"))

                        price = trade.get("price", 0)
                        size_usd = trade.get("size_usd", 0)
                        if won:
                            # Ganamos: pagamos price por share que vale $1
                            pnl = size_usd * ((1.0 / price) - 1)
                            result = "win"
                        else:
                            # Perdemos todo lo invertido
                            pnl = -size_usd
                            result = "loss"

                        # Actualizar en DB
                        await self.db.resolve_autotrade(cid, result, round(pnl, 2))
                        # Actualizar local
                        trade["resolved"] = True
                        trade["result"] = result
                        trade["pnl"] = round(pnl, 2)
                        del self._open_positions[cid]
                        resolved += 1

                        emoji = "✅" if result == "win" else "❌"
                        print(f"[AutoTrader] {emoji} Trade resuelto: {trade['coin']} "
                              f"{direction.upper()} → {result.upper()} PnL=${pnl:.2f}",
                              flush=True)

                    except Exception as e:
                        print(f"[AutoTrader] Error resolviendo {cid}: {e}", flush=True)

            if resolved:
                await self._load_today_trades()

        except Exception as e:
            print(f"[AutoTrader] Error en resolve_trades: {e}", flush=True)

    # ── Consultas de estado ───────────────────────────────────────────

    def get_status(self) -> dict:
        """Estado actual del autotrader para el dashboard."""
        daily_pnl = sum(t.get("pnl", 0) for t in self._trades_today if t.get("resolved"))
        daily_trades = len(self._trades_today)
        return {
            "enabled": self._enabled,
            "connected": self._client is not None,
            "open_positions": len(self._open_positions),
            "trades_today": daily_trades,
            "pnl_today": round(daily_pnl, 2),
        }

    async def test_connection(self) -> dict:
        """Probar conexión al CLOB con las credenciales actuales."""
        if not self._config.get("api_key") or not self._config.get("api_secret"):
            return {"connected": False, "error": "No hay credenciales configuradas."}

        try:
            # Primero probar que el CLOB responde
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{CLOB_HOST}/time")
                if resp.status_code != 200:
                    return {"connected": False, "error": f"CLOB respondió status {resp.status_code}"}

            # Probar autenticación creando el cliente
            if not self._client:
                self._init_clob_client()

            if not self._client:
                return {"connected": False, "error": "No se pudo crear el cliente CLOB."}

            # Intentar obtener API keys (valida credenciales) — síncrono, usar executor
            try:
                loop = asyncio.get_event_loop()
                api_keys = await loop.run_in_executor(None, self._client.get_api_keys)
                return {
                    "connected": True,
                    "api_keys_count": len(api_keys) if isinstance(api_keys, list) else 1,
                    "balance": None,
                    "note": "Credenciales válidas. Conexión exitosa.",
                }
            except Exception as e:
                err = str(e)
                if "401" in err or "403" in err or "Unauthorized" in err:
                    return {"connected": False, "error": "Credenciales inválidas. Verifica API Key/Secret/Passphrase."}
                # Si el error no es de auth, la conexión al menos funciona
                return {
                    "connected": True,
                    "balance": None,
                    "note": f"Conexión OK (advertencia: {err[:100]})",
                }
        except Exception as e:
            return {"connected": False, "error": str(e)}
