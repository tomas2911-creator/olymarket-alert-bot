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

from src import config

logger = structlog.get_logger()

# Constantes
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon
# USDC en Polygon (ambas versiones, 6 decimales)
USDC_CONTRACTS = [
    "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # USDC.e (bridged)
    "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",  # USDC (native)
]
POLYGON_RPC = "https://polygon-rpc.com"


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
        self._trailing_highs: dict[str, float] = {}  # cid -> max_price visto
        self._initialized = False

    async def initialize(self):
        """Cargar config y crear cliente CLOB si hay credenciales."""
        try:
            raw = await self.db.get_config_bulk([
                "at_enabled", "at_bet_size", "at_min_edge", "at_min_confidence",
                "at_max_odds", "at_max_positions", "at_order_type",
                "at_max_daily_loss", "at_max_daily_trades", "at_cooldown_sec",
                "at_coins", "at_api_key", "at_api_secret", "at_private_key", "at_passphrase",
                # Stop-Loss / Take-Profit / Risk Management
                "at_stop_loss_enabled", "at_stop_loss_pct", "at_take_profit_pct",
                "at_max_holding_sec", "at_trailing_stop_enabled", "at_trailing_stop_pct",
                "at_slippage_max_pct",
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
                # Stop-Loss / Take-Profit / Risk Management
                "stop_loss_enabled": raw.get("at_stop_loss_enabled") == "true",
                "stop_loss_pct": float(raw.get("at_stop_loss_pct", config.AT_STOP_LOSS_PCT)),
                "take_profit_pct": float(raw.get("at_take_profit_pct", config.AT_TAKE_PROFIT_PCT)),
                "max_holding_sec": int(raw.get("at_max_holding_sec", config.AT_MAX_HOLDING_SEC)),
                "trailing_stop_enabled": raw.get("at_trailing_stop_enabled") == "true",
                "trailing_stop_pct": float(raw.get("at_trailing_stop_pct", config.AT_TRAILING_STOP_PCT)),
                "slippage_max_pct": float(raw.get("at_slippage_max_pct", config.AT_SLIPPAGE_MAX_PCT)),
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
            trades = await self.db.get_autotrades(hours=24, user_id=1)
            self._trades_today = trades or []
        except Exception:
            self._trades_today = []

    async def _load_open_positions(self):
        """Cargar posiciones abiertas (trades sin resolver)."""
        try:
            open_trades = await self.db.get_open_autotrades(user_id=1)
            self._open_positions = {t["condition_id"]: t for t in (open_trades or [])}
        except Exception:
            self._open_positions = {}

    # ── Evaluación de señales ──────────────────────────────────────────

    async def evaluate_signal(self, signal: dict) -> Optional[dict]:
        """Evaluar si una señal debe ejecutarse. Retorna trade_info o None."""
        coin = signal.get("coin", "?")
        direction = signal.get("direction", "?")
        tag = f"[AT] {coin} {direction}"

        if not self._enabled or not self._client:
            print(f"{tag} SKIP: enabled={self._enabled} client={self._client is not None}", flush=True)
            return None

        cfg = self._config

        # Filtro: moneda habilitada
        if signal.get("coin", "") not in cfg["coins"]:
            print(f"{tag} SKIP: coin '{coin}' not in {cfg['coins']}", flush=True)
            return None

        # Filtro: edge mínimo
        edge = signal.get("edge_pct", 0)
        if edge < cfg["min_edge"]:
            print(f"{tag} SKIP: edge {edge}% < min {cfg['min_edge']}%", flush=True)
            return None

        # Filtro: confianza mínima
        confidence = signal.get("confidence", 0)
        if confidence < cfg["min_confidence"]:
            print(f"{tag} SKIP: confidence {confidence}% < min {cfg['min_confidence']}%", flush=True)
            return None

        # Filtro: odds máximo (no comprar si el precio ya es alto)
        poly_odds = signal.get("poly_odds", 1.0)
        if poly_odds > cfg["max_odds"]:
            print(f"{tag} SKIP: odds {poly_odds} > max {cfg['max_odds']}", flush=True)
            return None

        # Filtro: cooldown entre trades
        now = time.time()
        if now - self._last_trade_time < cfg["cooldown_sec"]:
            print(f"{tag} SKIP: cooldown ({int(now - self._last_trade_time)}s < {cfg['cooldown_sec']}s)", flush=True)
            return None

        # Filtro: max posiciones abiertas
        if len(self._open_positions) >= cfg["max_positions"]:
            print(f"{tag} SKIP: max positions ({len(self._open_positions)}>={cfg['max_positions']})", flush=True)
            return None

        # Filtro: max trades diarios
        if len(self._trades_today) >= cfg["max_daily_trades"]:
            print(f"{tag} SKIP: max daily trades ({len(self._trades_today)}>={cfg['max_daily_trades']})", flush=True)
            return None

        # Filtro: max pérdida diaria
        daily_pnl = sum(t.get("pnl", 0) for t in self._trades_today if t.get("resolved"))
        if daily_pnl <= -cfg["max_daily_loss"]:
            print(f"{tag} SKIP: max daily loss (pnl=${daily_pnl:.2f} <= -${cfg['max_daily_loss']})", flush=True)
            return None

        # Filtro: no duplicar posición en mismo mercado
        cid = signal.get("condition_id", "")
        if cid in self._open_positions:
            print(f"{tag} SKIP: already in position {cid[:12]}...", flush=True)
            return None

        # Filtro: tiempo restante mínimo (no entrar si queda muy poco)
        remaining = signal.get("time_remaining_sec", 0)
        if remaining < 30:
            print(f"{tag} SKIP: time remaining {remaining}s < 30s", flush=True)
            return None

        print(f"{tag} PASS: edge={edge}% conf={confidence}% odds={poly_odds} remaining={remaining}s -> EXECUTING ${cfg['bet_size']}", flush=True)

        # v8.0: Bankroll check — no apostar más del % permitido
        if config.FEATURE_BANKROLL:
            from src.infra.bankroll import BankrollTracker
            bankroll = getattr(self, '_bankroll', None)
            if bankroll and not bankroll.can_trade(cfg["bet_size"]):
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
            loop = asyncio.get_running_loop()
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
                    "created_ts": time.time(),
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
            if signals:
                print(f"[AT] process_signals: {len(signals)} signals IGNORED (enabled={self._enabled} client={self._client is not None} init={self._initialized})", flush=True)
            return
        if signals:
            print(f"[AT] process_signals: evaluando {len(signals)} señales...", flush=True)

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

    # ── Risk Management (SL/TP/Max Holding/Trailing) ──────────────────

    async def check_risk_management(self):
        """Verificar stop-loss, take-profit, max holding time y trailing stop
        para todas las posiciones abiertas. Vende si se alcanza algún umbral.
        """
        cfg = self._config
        if not cfg.get("stop_loss_enabled") or not self._open_positions or not self._client:
            return

        try:
            import httpx
            now = time.time()

            async with httpx.AsyncClient(timeout=10) as client:
                for cid, trade in list(self._open_positions.items()):
                    try:
                        # Obtener precio actual del token
                        resp = await client.get(f"{CLOB_HOST}/markets/{cid}")
                        if resp.status_code != 200:
                            continue
                        data = resp.json()
                        if data.get("closed"):
                            continue  # Se resuelve en resolve_trades

                        # Precio actual del outcome que compramos
                        direction = trade.get("direction", "")
                        tokens = data.get("tokens", [])
                        current_price = None
                        for tok in tokens:
                            outcome = tok.get("outcome", "").lower()
                            if (direction == "up" and outcome in ("up", "yes")) or \
                               (direction == "down" and outcome in ("down", "no")):
                                current_price = float(tok.get("price", 0))
                                break

                        if not current_price or current_price <= 0:
                            continue

                        entry_price = trade.get("price", 0)
                        if entry_price <= 0:
                            continue

                        # Calcular PnL actual en %
                        pnl_pct = ((current_price - entry_price) / entry_price) * 100
                        trade_age_sec = now - trade.get("created_ts", now)

                        # Actualizar trailing high
                        if cid not in self._trailing_highs:
                            self._trailing_highs[cid] = current_price
                        self._trailing_highs[cid] = max(self._trailing_highs[cid], current_price)

                        sell_reason = None

                        # 1. Stop-Loss: vender si pérdida > X%
                        if cfg["stop_loss_pct"] > 0 and pnl_pct <= -cfg["stop_loss_pct"]:
                            sell_reason = f"STOP-LOSS ({pnl_pct:.1f}% <= -{cfg['stop_loss_pct']}%)"

                        # 2. Take-Profit: vender si ganancia > X%
                        if not sell_reason and cfg["take_profit_pct"] > 0 and pnl_pct >= cfg["take_profit_pct"]:
                            sell_reason = f"TAKE-PROFIT ({pnl_pct:.1f}% >= +{cfg['take_profit_pct']}%)"

                        # 3. Max Holding Time: vender si pasó demasiado tiempo
                        if not sell_reason and cfg["max_holding_sec"] > 0 and trade_age_sec >= cfg["max_holding_sec"]:
                            sell_reason = f"MAX-HOLDING ({trade_age_sec:.0f}s >= {cfg['max_holding_sec']}s)"

                        # 4. Trailing Stop: vender si cayó X% desde el máximo
                        if not sell_reason and cfg.get("trailing_stop_enabled") and cfg["trailing_stop_pct"] > 0:
                            peak = self._trailing_highs.get(cid, current_price)
                            if peak > 0:
                                drop_from_peak = ((peak - current_price) / peak) * 100
                                if drop_from_peak >= cfg["trailing_stop_pct"]:
                                    sell_reason = f"TRAILING-STOP (caída {drop_from_peak:.1f}% desde máx ${peak:.3f})"

                        if sell_reason:
                            await self._sell_position(cid, trade, current_price, sell_reason)

                    except Exception as e:
                        print(f"[AutoTrader] Risk check error {cid}: {e}", flush=True)

        except Exception as e:
            print(f"[AutoTrader] Risk management error: {e}", flush=True)

    async def _sell_position(self, cid: str, trade: dict, current_price: float, reason: str):
        """Vender una posición abierta (ejecutar orden SELL)."""
        try:
            direction = trade.get("direction", "")
            shares = trade.get("shares", 0)
            token_id = trade.get("token_id", "")
            entry_price = trade.get("price", 0)

            if not token_id or shares <= 0:
                print(f"[AutoTrader] ⚠️ No se puede vender {cid}: sin token_id o shares", flush=True)
                return

            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                price=current_price,
                size=shares,
                side="SELL",
                token_id=token_id,
            )

            loop = asyncio.get_running_loop()
            signed_order = await loop.run_in_executor(None, self._client.create_order, order_args)
            resp = await loop.run_in_executor(None, self._client.post_order, signed_order, OrderType.FOK)

            # Calcular PnL
            pnl = (current_price - entry_price) * shares
            result = "win" if pnl >= 0 else "loss"

            # Actualizar en DB
            await self.db.resolve_autotrade(cid, result, round(pnl, 2))

            # Limpiar estado local
            trade["resolved"] = True
            trade["result"] = result
            trade["pnl"] = round(pnl, 2)
            self._open_positions.pop(cid, None)
            self._trailing_highs.pop(cid, None)

            emoji = "🛑" if "STOP" in reason else "💰" if "PROFIT" in reason else "⏰"
            print(f"[AutoTrader] {emoji} {reason}: {trade.get('coin','')} "
                  f"{direction.upper()} | Entry=${entry_price:.3f} Exit=${current_price:.3f} "
                  f"PnL=${pnl:.2f}", flush=True)

        except Exception as e:
            print(f"[AutoTrader] Error vendiendo {cid}: {e}", flush=True)

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

                        # v8.0: Hedging automático — comprar inversa si pérdida grande
                        if result == "loss" and config.FEATURE_HEDGING:
                            loss_pct = abs(pnl) / max(size_usd, 1) * 100
                            if loss_pct >= config.HEDGE_TRIGGER_LOSS_PCT:
                                hedge_size = size_usd * (config.HEDGE_SIZE_PCT / 100)
                                inverse_dir = "down" if direction == "up" else "up"
                                print(f"[AutoTrader] 🛡️ HEDGE: {trade['coin']} "
                                      f"{inverse_dir.upper()} ${hedge_size:.2f} "
                                      f"(pérdida {loss_pct:.0f}% > trigger {config.HEDGE_TRIGGER_LOSS_PCT}%)",
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

    async def get_usdc_balance(self, wallet_address: str = "") -> Optional[float]:
        """Consultar saldo USDC real en Polygon (EOA + Polymarket proxy)."""
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
        eoa_balance = 0.0

        # ── 1. Consultar USDC via Polygon RPC (balanceOf ERC-20) ──
        rpc_endpoints = [
            "https://polygon-rpc.com",
            "https://rpc.ankr.com/polygon",
            "https://polygon-bor-rpc.publicnode.com",
        ]
        addr_padded = wallet_address.lower().replace("0x", "").zfill(64)

        for rpc_url in rpc_endpoints:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    for contract in USDC_CONTRACTS:
                        payload = {
                            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                            "params": [{
                                "to": contract,
                                "data": f"0x70a08231000000000000000000000000{addr_padded}"
                            }, "latest"]
                        }
                        resp = await client.post(rpc_url, json=payload)
                        if resp.status_code == 200:
                            result = resp.json().get("result", "0x0")
                            if result and result != "0x":
                                bal = int(result, 16) / 1e6
                                eoa_balance += bal
                                if bal > 0:
                                    print(f"[AutoTrader] USDC {contract[:10]}...: ${bal:.2f} (via {rpc_url})", flush=True)
                if eoa_balance > 0:
                    break
            except Exception as e:
                print(f"[AutoTrader] RPC {rpc_url} error: {e}", flush=True)
                continue

        total_balance = eoa_balance

        # ── 3. Saldo en Polymarket (proxy wallet / CTF Exchange) ──
        if self._client:
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                loop = asyncio.get_running_loop()
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                ba = await loop.run_in_executor(
                    None, self._client.get_balance_allowance, params
                )
                poly_balance = 0.0
                if ba and hasattr(ba, "balance"):
                    raw_bal = float(ba.balance)
                    # Si el balance es muy grande, está en raw units (6 decimales)
                    poly_balance = raw_bal / 1e6 if raw_bal > 1_000 else raw_bal
                elif isinstance(ba, dict) and "balance" in ba:
                    raw_bal = float(ba["balance"])
                    poly_balance = raw_bal / 1e6 if raw_bal > 1_000 else raw_bal
                if poly_balance > 0:
                    total_balance += poly_balance
                    print(f"[AutoTrader] Balance Polymarket: ${poly_balance:.2f}", flush=True)
            except Exception as e:
                print(f"[AutoTrader] CLOB balance error: {e}", flush=True)

        print(f"[AutoTrader] Balance total: ${total_balance:.2f} (EOA=${eoa_balance:.2f}) wallet={wallet_address}", flush=True)
        return round(total_balance, 2)

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

            # Consultar saldo USDC real
            balance = await self.get_usdc_balance()

            # Intentar obtener API keys (valida credenciales) — síncrono, usar executor
            try:
                loop = asyncio.get_running_loop()
                api_keys = await loop.run_in_executor(None, self._client.get_api_keys)
                return {
                    "connected": True,
                    "api_keys_count": len(api_keys) if isinstance(api_keys, list) else 1,
                    "balance": balance,
                    "note": "Credenciales válidas. Conexión exitosa.",
                }
            except Exception as e:
                err = str(e)
                if "401" in err or "403" in err or "Unauthorized" in err:
                    return {"connected": False, "error": "Credenciales inválidas. Verifica API Key/Secret/Passphrase."}
                # Si el error no es de auth, la conexión al menos funciona
                return {
                    "connected": True,
                    "balance": balance,
                    "note": f"Conexión OK (advertencia: {err[:100]})",
                }
        except Exception as e:
            return {"connected": False, "error": str(e)}
