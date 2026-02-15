"""Motor de autotrading para alertas de insider — copy-trading de smart money.

Flujo:
1. Recibe AlertCandidate + Trade cuando se dispara una alerta
2. Filtra por configuración (score, odds, hit_rate, límites diarios)
3. Busca token_id del outcome apostado por el insider
4. Coloca orden BUY via py-clob-client
5. Registra trade en tabla alert_autotrades
6. Take Profit: vende automáticamente si el precio sube >= X% (configurable)
7. Se resuelve cuando el mercado cierra o se ejecuta take profit

Credenciales: usa SOLO wallet propia (aat_). No comparte wallet con Crypto Arb.
La config de trading es independiente con prefijo "aat_".
"""
import asyncio
import time
from datetime import datetime, timezone
from typing import Optional
import structlog

logger = structlog.get_logger()

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137


class AlertAutoTrader:
    """Ejecuta trades automáticos copiando apuestas de insiders detectados."""

    def __init__(self, db):
        self.db = db
        self._client = None
        self._enabled = False
        self._config = {}
        self._last_trade_time = 0.0
        self._trades_today: list[dict] = []
        self._trades_today_date: str = ""
        self._open_positions: dict[str, dict] = {}
        self._pending_confirmations: dict[str, dict] = {}
        self._initialized = False

    async def initialize(self):
        """Cargar config y crear cliente CLOB si hay credenciales."""
        try:
            # Config propia del alert autotrader (prefijo aat_)
            raw = await self.db.get_config_bulk([
                "aat_enabled", "aat_bet_size", "aat_min_score",
                "aat_max_odds", "aat_min_odds", "aat_max_positions",
                "aat_max_daily_trades", "aat_max_daily_loss",
                "aat_min_wallet_hit_rate", "aat_cooldown_hours",
                "aat_excluded_categories", "aat_require_smart_money",
                "aat_take_profit_enabled", "aat_take_profit_pct", "aat_stop_loss_pct",
                "aat_confirm_enabled", "aat_confirm_hours", "aat_confirm_min_pct", "aat_confirm_max_hours",
                # Credenciales propias del alert autotrader (NO usa las de Crypto Arb)
                "aat_api_key", "aat_api_secret", "aat_private_key", "aat_passphrase",
            ])
            api_key = raw.get("aat_api_key", "")
            api_secret = raw.get("aat_api_secret", "")
            private_key = raw.get("aat_private_key", "")
            passphrase = raw.get("aat_passphrase", "")

            self._config = {
                "enabled": raw.get("aat_enabled") == "true",
                "bet_size": float(raw.get("aat_bet_size", 10)),
                "min_score": int(raw.get("aat_min_score", 7)),
                "max_odds": float(raw.get("aat_max_odds", 0.80)),
                "min_odds": float(raw.get("aat_min_odds", 0.15)),
                "max_positions": int(raw.get("aat_max_positions", 5)),
                "max_daily_trades": int(raw.get("aat_max_daily_trades", 5)),
                "max_daily_loss": float(raw.get("aat_max_daily_loss", 50)),
                "min_wallet_hit_rate": float(raw.get("aat_min_wallet_hit_rate", 0)),
                "cooldown_hours": float(raw.get("aat_cooldown_hours", 6)),
                "excluded_categories": {
                    c.strip().lower()
                    for c in raw.get("aat_excluded_categories", "").split(",")
                    if c.strip()
                },
                "require_smart_money": raw.get("aat_require_smart_money") == "true",
                "take_profit_enabled": raw.get("aat_take_profit_enabled") == "true",
                "take_profit_pct": float(raw.get("aat_take_profit_pct", 0)),
                "stop_loss_pct": float(raw.get("aat_stop_loss_pct", 0)),
                "confirm_enabled": raw.get("aat_confirm_enabled") == "true",
                "confirm_hours": float(raw.get("aat_confirm_hours", 1)),
                "confirm_min_pct": float(raw.get("aat_confirm_min_pct", 3)),
                "confirm_max_hours": float(raw.get("aat_confirm_max_hours", 6)),
                "api_key": api_key,
                "api_secret": api_secret,
                "private_key": private_key,
                "passphrase": passphrase,
                "has_own_wallet": bool(raw.get("aat_private_key")),
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
                reason = " (sin credenciales — configura wallet en Alert Trading o Crypto Arb)"
            print(f"[AlertTrader] {status}{reason} | bet=${self._config['bet_size']} "
                  f"min_score>={self._config['min_score']}",
                  flush=True)
        except Exception as e:
            print(f"[AlertTrader] Error inicializando: {e}", flush=True)
            self._enabled = False

    def _init_clob_client(self):
        """Crear cliente CLOB con credenciales compartidas."""
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
            print("[AlertTrader] Cliente CLOB inicializado OK", flush=True)
        except ImportError:
            print("[AlertTrader] ERROR: py-clob-client no instalado", flush=True)
            self._client = None
        except Exception as e:
            print(f"[AlertTrader] Error creando cliente CLOB: {e}", flush=True)
            self._client = None

    async def reload_config(self):
        """Recargar config desde DB."""
        await self.initialize()

    async def _load_today_trades(self):
        """Cargar trades ejecutados hoy."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._trades_today_date != today:
            self._trades_today = []
            self._trades_today_date = today
        try:
            trades = await self.db.get_alert_autotrades(hours=24)
            self._trades_today = trades or []
        except Exception:
            self._trades_today = []

    async def _load_open_positions(self):
        """Cargar posiciones abiertas."""
        try:
            open_trades = await self.db.get_open_alert_autotrades()
            self._open_positions = {t["condition_id"]: t for t in (open_trades or [])}
        except Exception:
            self._open_positions = {}

    # ── Evaluación de alertas ────────────────────────────────────────

    async def evaluate_alert(self, candidate, trade) -> Optional[dict]:
        """Evaluar si una alerta debe copiarse como trade.
        candidate: AlertCandidate, trade: Trade
        Retorna trade_info dict o None.
        """
        if not self._enabled or not self._client or not self._initialized:
            return None

        cfg = self._config

        # Filtro: score mínimo
        if candidate.score < cfg["min_score"]:
            return None

        # Filtro: require smart money (wallet en watchlist)
        if cfg["require_smart_money"]:
            is_smart = any("Smart Money" in t for t in candidate.triggers)
            if not is_smart:
                return None

        # Filtro: hit rate mínimo de la wallet
        if cfg["min_wallet_hit_rate"] > 0 and candidate.wallet_hit_rate:
            if candidate.wallet_hit_rate < cfg["min_wallet_hit_rate"]:
                return None

        # Filtro: categoría excluida
        if cfg["excluded_categories"]:
            cat = (trade.market_category or "").lower()
            if cat and cat in cfg["excluded_categories"]:
                return None

        # Filtro: cooldown entre trades del mismo mercado
        cooldown_secs = cfg["cooldown_hours"] * 3600
        for t in self._trades_today:
            if t.get("condition_id") == trade.market_id:
                trade_time = t.get("created_at", "")
                if isinstance(trade_time, str) and trade_time:
                    try:
                        tt = datetime.fromisoformat(trade_time)
                        if (datetime.now(timezone.utc) - tt.replace(tzinfo=timezone.utc)).total_seconds() < cooldown_secs:
                            return None
                    except Exception:
                        pass

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
        if trade.market_id in self._open_positions:
            return None

        # Cooldown general entre trades
        now = time.time()
        if now - self._last_trade_time < 10:  # 10 seg mínimo entre trades
            return None

        return {
            "condition_id": trade.market_id,
            "market_slug": trade.market_slug,
            "market_question": trade.market_question,
            "wallet_address": trade.wallet_address,
            "insider_side": trade.side,
            "insider_outcome": trade.outcome,
            # Derivar outcome a comprar: si insider COMPRA Yes→compramos Yes, si VENDE Yes→compramos No
            "buy_outcome": trade.outcome if trade.side == "BUY" else ("No" if trade.outcome == "Yes" else "Yes"),
            "insider_size": trade.size,
            "insider_price": trade.price,
            "alert_score": candidate.score,
            "triggers": ", ".join(candidate.triggers[:5]),
            "wallet_hit_rate": candidate.wallet_hit_rate or 0,
            "bet_size": cfg["bet_size"],
            "category": trade.market_category or "",
        }

    # ── Ejecución de órdenes ─────────────────────────────────────────

    async def execute_trade(self, trade_info: dict) -> dict:
        """Ejecutar copy-trade en Polymarket CLOB."""
        if not self._client:
            return {"success": False, "error": "Cliente CLOB no inicializado"}

        cid = trade_info["condition_id"]
        outcome = trade_info["buy_outcome"]  # Outcome correcto considerando BUY/SELL del insider
        bet_size = trade_info["bet_size"]

        try:
            # Obtener token_id y precio actual del outcome
            token_id, current_price = await self._get_token_and_price(cid, outcome)
            if not token_id:
                return {"success": False, "error": f"No se encontró token_id para {outcome}"}

            price = current_price or trade_info["insider_price"]

            # Guard: precio inválido
            if not price or price <= 0:
                return {"success": False, "error": f"Precio inválido: {price}"}

            # Filtro: odds actuales del mercado (no del insider)
            cfg = self._config
            if price > cfg["max_odds"] or price < cfg["min_odds"]:
                return {"success": False, "error": f"Precio actual {price:.2f} fuera de rango [{cfg['min_odds']}, {cfg['max_odds']}]"}

            shares = round(bet_size / price, 2)

            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                price=price,
                size=shares,
                side="BUY",
                token_id=token_id,
            )

            # GTC (Good Till Cancel) para mercados largos
            loop = asyncio.get_event_loop()
            signed_order = await loop.run_in_executor(None, self._client.create_order, order_args)
            resp = await loop.run_in_executor(None, self._client.post_order, signed_order, OrderType.GTC)

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
                success = bool(resp)
                order_id = str(resp) if resp else ""

            trade_record = {
                "condition_id": cid,
                "order_id": order_id,
                "market_slug": trade_info["market_slug"],
                "market_question": trade_info["market_question"],
                "wallet_address": trade_info["wallet_address"],
                "insider_side": trade_info["insider_side"],
                "insider_outcome": outcome,
                "insider_size": trade_info["insider_size"],
                "alert_score": trade_info["alert_score"],
                "triggers": trade_info["triggers"],
                "side": "BUY",
                "outcome": outcome,
                "price": price,
                "size_usd": bet_size,
                "shares": shares,
                "token_id": token_id,
                "category": trade_info["category"],
                "wallet_hit_rate": trade_info["wallet_hit_rate"],
                "status": "filled" if success else "rejected",
                "error": error_msg if not success else None,
            }

            await self.db.record_alert_autotrade(trade_record)

            if success:
                self._last_trade_time = time.time()
                self._trades_today.append(trade_record)
                self._open_positions[cid] = trade_record
                print(f"[AlertTrader] ✅ COPY-TRADE: {outcome} en {trade_info['market_slug'][:40]} "
                      f"${bet_size} @ {price:.2f} (score={trade_info['alert_score']} "
                      f"insider=${trade_info['insider_size']:.0f}) order={order_id}",
                      flush=True)
                return {"success": True, "order_id": order_id, "trade": trade_record}
            else:
                print(f"[AlertTrader] ❌ Orden rechazada: {error_msg}", flush=True)
                return {"success": False, "error": error_msg}

        except Exception as e:
            error = str(e)
            print(f"[AlertTrader] ❌ Error ejecutando trade: {error}", flush=True)
            return {"success": False, "error": error}

    async def _get_token_and_price(self, condition_id: str, outcome: str) -> tuple:
        """Obtener token_id y precio actual del outcome (Yes/No)."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{CLOB_HOST}/markets/{condition_id}")
                if resp.status_code != 200:
                    return None, None
                data = resp.json()
                tokens = data.get("tokens", [])
                for token in tokens:
                    if token.get("outcome", "").lower() == outcome.lower():
                        return token.get("token_id", ""), float(token.get("price", 0))
                # Fallback: Yes = tokens[0], No = tokens[1]
                if tokens and outcome.lower() == "yes":
                    return tokens[0].get("token_id", ""), float(tokens[0].get("price", 0))
                elif len(tokens) > 1 and outcome.lower() == "no":
                    return tokens[1].get("token_id", ""), float(tokens[1].get("price", 0))
        except Exception as e:
            print(f"[AlertTrader] Error obteniendo token: {e}", flush=True)
        return None, None

    # ── Proceso completo: evaluar + ejecutar ─────────────────────────

    async def process_alert(self, candidate, trade):
        """Evaluar alerta y ejecutar trade si pasa filtros.
        Llamado desde main.py después de enviar alerta a Telegram.
        Si confirmación está activa, encola en vez de ejecutar.
        """
        if not self._enabled or not self._client or not self._initialized:
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._trades_today_date != today:
            await self._load_today_trades()

        trade_info = await self.evaluate_alert(candidate, trade)
        if not trade_info:
            return

        cfg = self._config
        if cfg.get("confirm_enabled") and cfg.get("confirm_hours", 0) > 0:
            # Encolar para confirmar después
            cid = trade_info["condition_id"]
            if cid not in self._pending_confirmations:
                trade_info["queued_at"] = time.time()
                trade_info["entry_price_at_queue"] = trade_info.get("insider_price", 0)
                self._pending_confirmations[cid] = trade_info
                print(f"[AlertTrader] ⏳ ENCOLADO para confirmación: "
                      f"{trade_info['market_slug'][:40]} (espera {cfg['confirm_hours']}h, min +{cfg['confirm_min_pct']}%)",
                      flush=True)
        else:
            # Ejecutar inmediatamente
            result = await self.execute_trade(trade_info)
            if result.get("success"):
                logger.info("alert_copy_trade",
                            market=trade.market_slug,
                            score=candidate.score,
                            size=trade_info["bet_size"])

    async def check_pending_confirmations(self):
        """Revisar alertas pendientes de confirmación por price impact.
        Si el precio subió >= confirm_min_pct después de confirm_hours → ejecutar.
        Si pasó confirm_max_hours sin confirmar → descartar.
        """
        cfg = self._config
        if not cfg.get("confirm_enabled") or not self._pending_confirmations or not self._client:
            return

        confirm_secs = cfg.get("confirm_hours", 1) * 3600
        max_secs = cfg.get("confirm_max_hours", 6) * 3600
        min_pct = cfg.get("confirm_min_pct", 3)
        now = time.time()

        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                for cid, info in list(self._pending_confirmations.items()):
                    queued_at = info.get("queued_at", 0)
                    elapsed = now - queued_at
                    entry_price = info.get("entry_price_at_queue", 0)

                    # Descartar si pasó el tiempo máximo
                    if elapsed > max_secs:
                        del self._pending_confirmations[cid]
                        print(f"[AlertTrader] ❌ DESCARTADO (timeout {cfg['confirm_max_hours']}h): "
                              f"{info.get('market_slug', cid)[:40]}", flush=True)
                        continue

                    # Solo evaluar si pasó el tiempo mínimo de espera
                    if elapsed < confirm_secs:
                        continue

                    if not entry_price or entry_price <= 0:
                        del self._pending_confirmations[cid]
                        continue

                    # Consultar precio actual
                    try:
                        outcome = info.get("buy_outcome", "Yes")
                        resp = await client.get(f"{CLOB_HOST}/markets/{cid}")
                        if resp.status_code != 200:
                            continue
                        data = resp.json()
                        if data.get("closed"):
                            del self._pending_confirmations[cid]
                            continue

                        tokens = data.get("tokens", [])
                        current_price = None
                        for tk in tokens:
                            if tk.get("outcome", "").lower() == outcome.lower():
                                current_price = float(tk.get("price", 0))
                                break
                        if not current_price:
                            continue

                        gain_pct = ((current_price - entry_price) / entry_price) * 100

                        if gain_pct >= min_pct:
                            # Confirmado — ejecutar trade al precio actual
                            del self._pending_confirmations[cid]
                            result = await self.execute_trade(info)
                            if result.get("success"):
                                print(f"[AlertTrader] ✅ CONFIRMADO (+{gain_pct:.1f}%): "
                                      f"{info.get('market_slug', cid)[:40]} "
                                      f"entrada={entry_price:.2f} → ahora={current_price:.2f}",
                                      flush=True)
                        elif gain_pct <= -min_pct:
                            # Price impact negativo fuerte — descartar
                            del self._pending_confirmations[cid]
                            print(f"[AlertTrader] ❌ DESCARTADO ({gain_pct:+.1f}%): "
                                  f"{info.get('market_slug', cid)[:40]}", flush=True)

                    except Exception as e:
                        print(f"[AlertTrader] Error confirmando {cid}: {e}", flush=True)

        except Exception as e:
            print(f"[AlertTrader] Error en check_pending_confirmations: {e}", flush=True)

    # ── Take Profit / Stop Loss ────────────────────────────────────────

    async def check_take_profits(self):
        """Revisar posiciones abiertas y vender si alcanzan take profit o stop loss."""
        cfg = self._config
        if not cfg.get("take_profit_enabled") or not self._open_positions or not self._client:
            return

        tp_pct = cfg.get("take_profit_pct", 0)
        sl_pct = cfg.get("stop_loss_pct", 0)
        if tp_pct <= 0 and sl_pct <= 0:
            return

        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                for cid, trade in list(self._open_positions.items()):
                    try:
                        entry_price = trade.get("price", 0)
                        if not entry_price or entry_price <= 0:
                            continue
                        token_id = trade.get("token_id", "")
                        if not token_id:
                            continue

                        resp = await client.get(f"{CLOB_HOST}/markets/{cid}")
                        if resp.status_code != 200:
                            continue
                        data = resp.json()
                        if data.get("closed"):
                            continue  # resolve_trades se encarga de estos

                        tokens = data.get("tokens", [])
                        current_price = None
                        for tk in tokens:
                            if tk.get("token_id") == token_id:
                                current_price = float(tk.get("price", 0))
                                break
                            if tk.get("outcome", "").lower() == trade.get("outcome", "").lower():
                                current_price = float(tk.get("price", 0))
                                break

                        if not current_price or current_price <= 0:
                            continue

                        gain_pct = ((current_price - entry_price) / entry_price) * 100
                        action = None

                        if tp_pct > 0 and gain_pct >= tp_pct:
                            action = "take_profit"
                        elif sl_pct > 0 and gain_pct <= -sl_pct:
                            action = "stop_loss"

                        if not action:
                            continue

                        sell_result = await self._sell_position(trade, current_price)
                        if sell_result.get("success"):
                            pnl = trade.get("size_usd", 0) * (current_price / entry_price - 1)
                            await self.db.resolve_alert_autotrade(cid, action, round(pnl, 2))
                            del self._open_positions[cid]
                            emoji = "💰" if action == "take_profit" else "🛑"
                            label = "TAKE PROFIT" if action == "take_profit" else "STOP LOSS"
                            print(f"[AlertTrader] {emoji} {label}: "
                                  f"{trade.get('market_slug', cid)[:40]} "
                                  f"entrada={entry_price:.2f} → venta={current_price:.2f} "
                                  f"({gain_pct:+.1f}%) PnL=${pnl:.2f}",
                                  flush=True)
                            await self._load_today_trades()

                    except Exception as e:
                        print(f"[AlertTrader] Error check_take_profit {cid}: {e}", flush=True)

        except Exception as e:
            print(f"[AlertTrader] Error en check_take_profits: {e}", flush=True)

    async def _sell_position(self, trade: dict, sell_price: float) -> dict:
        """Vender una posición abierta (SELL en CLOB)."""
        try:
            token_id = trade.get("token_id", "")
            shares = trade.get("shares", 0)
            if not token_id or not shares:
                return {"success": False, "error": "Sin token_id o shares"}

            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                price=sell_price,
                size=shares,
                side="SELL",
                token_id=token_id,
            )

            loop = asyncio.get_event_loop()
            signed_order = await loop.run_in_executor(None, self._client.create_order, order_args)
            resp = await loop.run_in_executor(None, self._client.post_order, signed_order, OrderType.GTC)

            if isinstance(resp, dict):
                success = resp.get("success", False) or resp.get("orderID") is not None
                order_id = resp.get("orderID", resp.get("order_id", ""))
            elif hasattr(resp, "success"):
                success = resp.success
                order_id = getattr(resp, "orderID", "")
            else:
                success = bool(resp)
                order_id = str(resp) if resp else ""

            return {"success": success, "order_id": order_id}

        except Exception as e:
            print(f"[AlertTrader] Error vendiendo posición: {e}", flush=True)
            return {"success": False, "error": str(e)}

    # ── Resolución de trades ─────────────────────────────────────────

    async def resolve_trades(self):
        """Resolver trades abiertos: take profit + resolución de mercados cerrados."""
        if not self._open_positions:
            return

        # Primero: check take profit / stop loss
        await self.check_take_profits()

        try:
            import httpx
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

                        our_outcome = trade.get("outcome", "")
                        won = our_outcome.lower() == winning_outcome.lower()

                        price = trade.get("price", 0)
                        size_usd = trade.get("size_usd", 0)
                        if won:
                            pnl = size_usd * ((1.0 / price) - 1)
                            result = "win"
                        else:
                            pnl = -size_usd
                            result = "loss"

                        await self.db.resolve_alert_autotrade(cid, result, round(pnl, 2))
                        del self._open_positions[cid]
                        resolved += 1

                        emoji = "✅" if result == "win" else "❌"
                        print(f"[AlertTrader] {emoji} Trade resuelto: "
                              f"{trade.get('market_slug', cid)[:40]} → {result.upper()} PnL=${pnl:.2f}",
                              flush=True)

                    except Exception as e:
                        print(f"[AlertTrader] Error resolviendo {cid}: {e}", flush=True)

            if resolved:
                await self._load_today_trades()

        except Exception as e:
            print(f"[AlertTrader] Error en resolve_trades: {e}", flush=True)

    # ── Estado ───────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Estado actual para el dashboard."""
        daily_pnl = sum(t.get("pnl", 0) for t in self._trades_today if t.get("resolved"))
        return {
            "enabled": self._enabled,
            "connected": self._client is not None,
            "open_positions": len(self._open_positions),
            "pending_confirmations": len(self._pending_confirmations),
            "trades_today": len(self._trades_today),
            "pnl_today": round(daily_pnl, 2),
        }
