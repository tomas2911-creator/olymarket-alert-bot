"""Motor de autotrading para crypto arb — ejecuta trades reales en Polymarket CLOB.

Flujo:
1. Recibe señales del CryptoArbDetector
2. Filtra por configuración del usuario (edge, confianza, monedas, límites)
3. Coloca órdenes via py-clob-client
4. Registra trades en DB
5. Monitorea resultado
"""
import asyncio
import os
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
        self._user_id = 1  # user_id para cargar config
        self._poly_web3_service = None  # PolyWeb3Service para auto-claim
        self._last_claim_time = 0.0  # timestamp del último intento de claim
        self._processing_signals = False  # Lock para evitar evaluación concurrente
        self._failed_cids: dict[str, float] = {}  # cid -> timestamp: blacklist señales fallidas (evita retry loop)
        # Maker orders state
        self._open_maker_orders: dict[str, dict] = {}  # order_id -> order_info
        self._maker_stats = {"placed": 0, "filled": 0, "cancelled": 0, "requoted": 0}

    async def initialize(self, user_id: int = None):
        """Cargar config y crear cliente CLOB si hay credenciales."""
        if user_id is not None:
            self._user_id = user_id
        try:
            raw = await self.db.get_config_bulk([
                "at_enabled", "at_bet_size", "at_min_edge", "at_min_confidence",
                "at_max_odds", "at_max_positions", "at_order_type",
                "at_max_daily_loss", "at_max_daily_trades", "at_cooldown_sec",
                "at_coins", "at_api_key", "at_api_secret", "at_private_key", "at_passphrase",
                "at_funder_address", "at_min_score",
                "at_use_score_strategy", "at_use_early_entry", "at_early_entry_bet_size",
                # Stop-Loss / Take-Profit / Risk Management
                "at_stop_loss_enabled", "at_stop_loss_pct", "at_take_profit_pct",
                "at_max_holding_sec", "at_trailing_stop_enabled", "at_trailing_stop_pct",
                "at_slippage_max_pct",
                "at_ee_take_profit_enabled", "at_ee_take_profit_pct",
                # Sniper
                "at_use_sniper", "at_sniper_bet_size", "at_sniper_slippage_pct",
                "at_sniper_min_move_pct", "at_sniper_max_buy_price",
                "at_sniper_cooldown_sec", "at_sniper_entry_delay_sec",
                "at_sniper_entry_max_sec", "at_sniper_tp_enabled", "at_sniper_tp_pct",
                # Maker Orders
                "at_maker_spread_offset", "at_maker_max_open_orders",
                "at_maker_requote_threshold", "at_maker_fill_timeout_sec",
                "at_hybrid_score_threshold",
            ], user_id=self._user_id)
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
                "funder_address": raw.get("at_funder_address", ""),
                # Stop-Loss / Take-Profit / Risk Management
                "stop_loss_enabled": raw.get("at_stop_loss_enabled") == "true",
                "stop_loss_pct": float(raw.get("at_stop_loss_pct", config.AT_STOP_LOSS_PCT)),
                "take_profit_pct": float(raw.get("at_take_profit_pct", config.AT_TAKE_PROFIT_PCT)),
                "max_holding_sec": int(raw.get("at_max_holding_sec", config.AT_MAX_HOLDING_SEC)),
                "trailing_stop_enabled": raw.get("at_trailing_stop_enabled") == "true",
                "trailing_stop_pct": float(raw.get("at_trailing_stop_pct", config.AT_TRAILING_STOP_PCT)),
                "slippage_max_pct": float(raw.get("at_slippage_max_pct", config.AT_SLIPPAGE_MAX_PCT)),
                "min_score": float(raw.get("at_min_score", 0)),
                "use_score_strategy": raw.get("at_use_score_strategy", "true") == "true",
                "use_early_entry": raw.get("at_use_early_entry", "false") == "true",
                "early_entry_bet_size": float(raw.get("at_early_entry_bet_size", config.EARLY_ENTRY_BET_SIZE)),
                "ee_take_profit_enabled": raw.get("at_ee_take_profit_enabled") == "true",
                "ee_take_profit_pct": float(raw.get("at_ee_take_profit_pct", 40)),
                # Sniper
                "use_sniper": raw.get("at_use_sniper", "false") == "true",
                "sniper_bet_size": float(raw.get("at_sniper_bet_size", 3)),
                "sniper_slippage_pct": float(raw.get("at_sniper_slippage_pct", 5)),
                "sniper_min_move_pct": float(raw.get("at_sniper_min_move_pct", 0.03)),
                "sniper_max_buy_price": float(raw.get("at_sniper_max_buy_price", 0.60)),
                "sniper_cooldown_sec": int(raw.get("at_sniper_cooldown_sec", 0)),
                "sniper_entry_delay_sec": int(raw.get("at_sniper_entry_delay_sec", 55)),
                "sniper_entry_max_sec": int(raw.get("at_sniper_entry_max_sec", 150)),
                "sniper_tp_enabled": raw.get("at_sniper_tp_enabled") == "true",
                "sniper_tp_pct": float(raw.get("at_sniper_tp_pct", 30)),
                # Maker Orders
                "maker_spread_offset": float(raw.get("at_maker_spread_offset", config.AT_MAKER_SPREAD_OFFSET)),
                "maker_max_open_orders": int(raw.get("at_maker_max_open_orders", config.AT_MAKER_MAX_OPEN_ORDERS)),
                "maker_requote_threshold": float(raw.get("at_maker_requote_threshold", config.AT_MAKER_REQUOTE_THRESHOLD)),
                "maker_fill_timeout_sec": int(raw.get("at_maker_fill_timeout_sec", config.AT_MAKER_FILL_TIMEOUT_SEC)),
                "hybrid_score_threshold": float(raw.get("at_hybrid_score_threshold", config.AT_HYBRID_SCORE_THRESHOLD)),
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
            min_sc = self._config.get('min_score', 0)
            score_info = f" score>={min_sc}" if min_sc > 0 else ""
            strats = []
            if self._config.get('use_score_strategy'): strats.append('score')
            if self._config.get('use_early_entry'): strats.append('early')
            if self._config.get('use_sniper'): strats.append('sniper')
            strat_info = f" strategies={'+'.join(strats)}" if strats else ""
            early_bet = self._config.get('early_entry_bet_size', 0)
            early_info = f" early_bet=${early_bet}" if self._config.get('use_early_entry') else ""
            sniper_info = ""
            if self._config.get('use_sniper'):
                sniper_info = (f" sniper_bet=${self._config.get('sniper_bet_size', 3)}"
                               f" sniper_slip={self._config.get('sniper_slippage_pct', 5)}%"
                               f" sniper_cd={self._config.get('sniper_cooldown_sec', 0)}s")
            print(f"[AutoTrader] {status}{reason} | bet=${self._config['bet_size']} "
                  f"edge>={self._config['min_edge']}% conf>={self._config['min_confidence']}%{score_info}{strat_info}{early_info}{sniper_info}",
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
            pk = self._config["private_key"]
            if pk and not pk.startswith("0x"):
                pk = "0x" + pk
            funder = self._config.get("funder_address", "") or None
            self._client = ClobClient(
                CLOB_HOST,
                key=pk,
                chain_id=CHAIN_ID,
                signature_type=2,  # Browser wallet proxy (Rabby/MetaMask via Polymarket)
                funder=funder,
                creds=creds,
            )
            print(f"[AutoTrader] Cliente CLOB inicializado OK (funder={'set: '+funder[:10]+'...' if funder else 'NOT SET - orders will fail!'})", flush=True)
            if not funder:
                print("[AutoTrader] ⚠️ FUNDER ADDRESS no configurada. Ve a polymarket.com/settings, copia tu Proxy Wallet Address, y pégala en el dashboard.", flush=True)
            else:
                # Diagnosticar y configurar allowances
                self._setup_allowances()
        except ImportError:
            print("[AutoTrader] ERROR: py-clob-client no instalado. pip install py-clob-client", flush=True)
            self._client = None
        except Exception as e:
            print(f"[AutoTrader] Error creando cliente CLOB: {e}", flush=True)
            self._client = None

    def _setup_allowances(self):
        """Diagnosticar token allowances para trading."""
        if not self._client:
            return
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams
            # Diagnosticar: ver balance y allowance actuales
            try:
                params = BalanceAllowanceParams(asset_type="COLLATERAL")
                bal = self._client.get_balance_allowance(params)
                balance = bal.get("balance", "?") if isinstance(bal, dict) else getattr(bal, "balance", "?")
                allowance = bal.get("allowance", "?") if isinstance(bal, dict) else getattr(bal, "allowance", "?")
                print(f"[AutoTrader] Allowance COLLATERAL: balance={balance} allowance={allowance}", flush=True)
                # Verificar si allowance es 0 o muy bajo
                try:
                    allow_val = float(str(allowance))
                    if allow_val == 0:
                        print("[AutoTrader] ⚠️ ALLOWANCE = 0. Ve a polymarket.com, intenta hacer un trade manual, y firma 'Enable Trading' + 'Approve Tokens'.", flush=True)
                except (ValueError, TypeError):
                    pass
            except ImportError:
                print("[AutoTrader] BalanceAllowanceParams no disponible en esta versión de py-clob-client", flush=True)
            except Exception as e:
                print(f"[AutoTrader] Allowance check error: {e}", flush=True)
        except ImportError:
            print("[AutoTrader] Allowance check: BalanceAllowanceParams no encontrado", flush=True)
        except Exception as e:
            print(f"[AutoTrader] Allowance setup error: {e}", flush=True)

    async def reload_config(self, user_id: int = None):
        """Recargar config desde DB (llamado cuando se guarda config desde dashboard)."""
        await self.initialize(user_id=user_id)

    def reset_state(self):
        """Resetear estado en memoria (posiciones abiertas, trades de hoy)."""
        self._open_positions = {}
        self._trades_today = []
        self._trades_today_date = ""
        self._last_trade_time = 0.0
        self._trailing_highs = {}
        self._failed_cids = {}
        print("[AutoTrader] Estado reseteado: posiciones=0, trades_today=0", flush=True)

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

        # Filtro: estrategia habilitada
        strategy = signal.get("strategy", "score")
        if strategy == "early_entry" and not cfg.get("use_early_entry", True):
            return None
        if strategy in ("score", "divergence") and not cfg.get("use_score_strategy", True):
            return None
        if strategy == "sniper" and not cfg.get("use_sniper", False):
            return None

        # Filtro: moneda habilitada
        if signal.get("coin", "") not in cfg["coins"]:
            print(f"{tag} SKIP: coin '{coin}' not in {cfg['coins']}", flush=True)
            return None

        # Filtro: score mínimo (0 = desactivado)
        score_details = signal.get("score_details", {})
        signal_score = score_details.get("score", 0) if isinstance(score_details, dict) else 0
        if cfg.get("min_score", 0) > 0 and signal_score < cfg["min_score"]:
            print(f"{tag} SKIP: score {signal_score:.2f} < min {cfg['min_score']}", flush=True)
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

        # Filtro: cooldown entre trades (sniper tiene su propio cooldown)
        now = time.time()
        cooldown = cfg.get("sniper_cooldown_sec", 0) if strategy == "sniper" else cfg["cooldown_sec"]
        if cooldown > 0 and now - self._last_trade_time < cooldown:
            print(f"{tag} SKIP: cooldown ({int(now - self._last_trade_time)}s < {cooldown}s)", flush=True)
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

        # Filtro: no duplicar posición en mismo mercado (por event_slug, NO por condition_id)
        # Up y Down del mismo mercado tienen distinto condition_id pero MISMO event_slug
        cid = signal.get("condition_id", "")
        event_slug = signal.get("event_slug", "")
        if cid in self._open_positions:
            print(f"{tag} SKIP: already in position (cid) {cid[:12]}...", flush=True)
            return None
        if event_slug:
            for pos in self._open_positions.values():
                if pos.get("event_slug") == event_slug:
                    print(f"{tag} SKIP: already in position for market {event_slug} (lado opuesto)", flush=True)
                    return None

        # Filtro: señal que falló recientemente (evitar retry loop infinito)
        if cid in self._failed_cids:
            fail_age = time.time() - self._failed_cids[cid]
            if fail_age < 120:  # Blacklist por 2 minutos
                print(f"{tag} SKIP: señal fallida hace {int(fail_age)}s (blacklist 120s)", flush=True)
                return None
            else:
                del self._failed_cids[cid]

        # Filtro: tiempo restante mínimo (no entrar si queda muy poco)
        remaining = signal.get("time_remaining_sec", 0)
        if remaining < 30:
            print(f"{tag} SKIP: time remaining {remaining}s < 30s", flush=True)
            return None

        # Bet size diferenciado por estrategia
        bet_size = cfg["bet_size"]
        if strategy == "early_entry":
            bet_size = cfg.get("early_entry_bet_size", cfg["bet_size"])
        elif strategy == "sniper":
            bet_size = cfg.get("sniper_bet_size", cfg["bet_size"])

        print(f"{tag} PASS [{strategy}]: edge={edge}% conf={confidence}% odds={poly_odds} remaining={remaining}s -> EXECUTING ${bet_size}", flush=True)

        # v8.0: Bankroll check — no apostar más del % permitido
        if config.FEATURE_BANKROLL:
            from src.infra.bankroll import BankrollTracker
            bankroll = getattr(self, '_bankroll', None)
            if bankroll and not bankroll.can_trade(bet_size):
                return None

        # Sniper siempre usa FOK (market) para ejecución inmediata
        order_type = "market" if strategy == "sniper" else cfg["order_type"]

        return {
            "condition_id": cid,
            "coin": signal["coin"],
            "direction": signal["direction"],
            "edge_pct": edge,
            "confidence": confidence,
            "poly_odds": poly_odds,
            "fair_odds": signal.get("fair_odds", 0),
            "bet_size": bet_size,
            "order_type": order_type,
            "spot_price": signal.get("spot_price", 0),
            "event_slug": signal.get("event_slug", ""),
            "market_question": signal.get("market_question", ""),
            "time_remaining_sec": remaining,
            "strategy": strategy,
        }

    # ── Ejecución de órdenes ──────────────────────────────────────────

    async def execute_trade(self, trade_info: dict) -> dict:
        """Ejecutar un trade en Polymarket CLOB.
        Soporta 4 modos: market (FOK), limit (GTC), maker (post-only GTC), hybrid.
        Retorna dict con resultado de la orden.
        """
        if not self._client:
            return {"success": False, "error": "Cliente CLOB no inicializado"}

        cid = trade_info["condition_id"]
        direction = trade_info["direction"]
        bet_size = trade_info["bet_size"]
        price = trade_info["poly_odds"]
        order_mode = trade_info.get("order_type", "limit")

        # ── Modo MAKER: post-only GTC ──
        if order_mode == "maker":
            return await self._execute_maker_order(trade_info)

        # ── Modo HYBRID: maker con sesgo direccional ──
        if order_mode == "hybrid":
            return await self._execute_hybrid_order(trade_info)

        try:
            # Obtener token_id del outcome correcto
            token_result = await self._get_token_id(cid, direction)
            if not token_result:
                return {"success": False, "error": f"No se encontró token_id para {direction}"}
            token_id, selected_outcome = token_result
            print(f"[AutoTrader] Token seleccionado: direction={direction} → outcome='{selected_outcome}' token={token_id[:16]}...", flush=True)

            # Redondear precio a 2 decimales (Polymarket usa centavos)
            price = round(price, 2)
            if price <= 0 or price >= 1:
                return {"success": False, "error": f"Precio inválido: {price}"}

            # FOK: agregar slippage para encontrar liquidez en el order book
            # El price en FOK BUY es el MÁXIMO que estamos dispuestos a pagar
            is_fok = order_mode == "market"
            strategy = trade_info.get("strategy", "score")
            if is_fok:
                # Sniper usa su propio slippage configurable
                if strategy == "sniper":
                    slippage_pct = self._config.get("sniper_slippage_pct", 5.0)
                else:
                    slippage_pct = self._config.get("slippage_max_pct", 3.0)
                slippage = round(slippage_pct / 100, 2)  # ej: 5% → 0.05
                order_price = min(round(price + slippage, 2), 0.99)
            else:
                order_price = price

            # Calcular shares con precisión correcta para CLOB:
            # - maker_amount (USDC = shares * price) → max 2 decimales
            # - taker_amount (shares) → max 4 decimales
            # Usa Decimal para evitar errores de floating-point que el CLOB rechaza
            import math
            from decimal import Decimal, ROUND_DOWN

            MIN_CLOB_SHARES = Decimal('5')  # Mínimo requerido por Polymarket CLOB

            d_price = Decimal(str(order_price))
            d_raw = Decimal(str(bet_size)) / d_price

            # Probar precisiones de 4 a 0 decimales hasta que maker_amount tenga <= 2 decimales
            d_shares = Decimal('0')
            for decimals in [4, 3, 2, 1, 0]:
                q = Decimal(10) ** -decimals
                d_candidate = d_raw.quantize(q, rounding=ROUND_DOWN)
                d_maker = d_candidate * d_price
                if d_maker == d_maker.quantize(Decimal('0.01')):
                    d_shares = d_candidate
                    break

            # Enforce mínimo de shares del CLOB (5 shares)
            if d_shares < MIN_CLOB_SHARES:
                d_shares = MIN_CLOB_SHARES
                bet_size = float((d_shares * d_price).quantize(Decimal('0.01')))
                print(f"[AutoTrader] ⚠️ Shares ajustadas al mínimo CLOB: {d_shares} (bet=${bet_size:.2f})", flush=True)

            shares = float(d_shares)
            if shares <= 0:
                self._failed_cids[cid] = time.time()
                return {"success": False, "error": f"No se pudo calcular shares válidas para price={order_price} bet={bet_size}"}

            print(f"[AutoTrader] Order: price={order_price} shares={shares} usdc={round(shares*order_price,2)} type={trade_info['order_type']}{f' (slippage +{slippage_pct}%)' if is_fok else ''}", flush=True)

            # Crear y enviar orden
            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                price=order_price,
                size=shares,
                side="BUY",
                token_id=token_id,
            )

            order_type = OrderType.FOK if trade_info["order_type"] == "market" else OrderType.GTC
            # py-clob-client es síncrono — ejecutar en executor para no bloquear event loop
            loop = asyncio.get_running_loop()
            signed_order = await loop.run_in_executor(None, self._client.create_order, order_args)

            # Intentar enviar orden; si FOK falla por liquidez, reintentar como GTC
            used_order_type = order_type
            try:
                resp = await loop.run_in_executor(None, self._client.post_order, signed_order, order_type)
            except Exception as fok_err:
                fok_msg = str(fok_err).lower()
                if order_type == OrderType.FOK and "fully filled" in fok_msg:
                    print(f"[AutoTrader] FOK sin liquidez, reintentando como GTC limit...", flush=True)
                    # Crear nueva orden con el mismo precio (limit order se queda en el book)
                    gtc_args = OrderArgs(
                        price=order_price,
                        size=shares,
                        side="BUY",
                        token_id=token_id,
                    )
                    signed_order = await loop.run_in_executor(None, self._client.create_order, gtc_args)
                    resp = await loop.run_in_executor(None, self._client.post_order, signed_order, OrderType.GTC)
                    used_order_type = OrderType.GTC
                else:
                    raise  # Re-lanzar si es otro error

            # Log respuesta completa para debugging
            print(f"[AutoTrader] post_order response ({used_order_type}): {resp}", flush=True)

            # Parsear respuesta
            success = False
            order_id = ""
            error_msg = ""
            resp_status = ""

            if isinstance(resp, dict):
                order_id = resp.get("orderID", resp.get("order_id", "")) or ""
                resp_status = resp.get("status", "")
                # Success solo si hay orderID no-vacío
                success = bool(order_id) and resp.get("success", True)
                if not success:
                    error_msg = resp.get("errorMsg", resp.get("error", str(resp)))
            elif hasattr(resp, "success"):
                success = resp.success
                order_id = getattr(resp, "orderID", "") or ""
                resp_status = getattr(resp, "status", "")
                error_msg = getattr(resp, "errorMsg", "")
            else:
                order_id = str(resp) if resp else ""
                success = bool(order_id)

            # ── GTC 'live': la orden está en el orderbook pero NO llenada aún ──
            # Hacer polling para verificar si se llena, y cancelar si no.
            if success and resp_status.lower() == "live" and order_id:
                print(f"[AutoTrader] ⏳ Orden GTC en orderbook (status=live), esperando fill...", flush=True)
                filled = False
                for attempt in range(8):  # 8 intentos × 5s = 40s máximo
                    await asyncio.sleep(5)
                    try:
                        order_info = await loop.run_in_executor(
                            None, self._client.get_order, order_id
                        )
                        current_status = ""
                        if isinstance(order_info, dict):
                            current_status = order_info.get("status", "")
                        elif hasattr(order_info, "status"):
                            current_status = getattr(order_info, "status", "")
                        print(f"[AutoTrader]   polling {attempt+1}/8: status={current_status}", flush=True)
                        if current_status.lower() == "matched":
                            filled = True
                            break
                        elif current_status.lower() in ("cancelled", "expired", ""):
                            break  # Ya no está activa
                    except Exception as poll_err:
                        print(f"[AutoTrader]   polling error: {poll_err}", flush=True)
                        break

                if not filled:
                    # Intentar cancelar la orden huérfana
                    try:
                        await loop.run_in_executor(None, self._client.cancel, order_id)
                        print(f"[AutoTrader] ❌ Orden GTC no llenada, CANCELADA: {order_id[:16]}...", flush=True)
                    except Exception as cancel_err:
                        print(f"[AutoTrader] ⚠️ Error cancelando orden GTC: {cancel_err}", flush=True)
                    success = False
                    error_msg = "GTC order not filled within timeout, cancelled"
                else:
                    print(f"[AutoTrader] ✅ Orden GTC llenada (matched)!", flush=True)

            if success:
                self._last_trade_time = time.time()
                trade_record = {
                    "condition_id": cid,
                    "order_id": order_id,
                    "coin": trade_info["coin"],
                    "direction": direction,
                    "side": "BUY",
                    "price": order_price,
                    "size_usd": bet_size,
                    "shares": shares,
                    "token_id": token_id,
                    "edge_pct": trade_info["edge_pct"],
                    "confidence": trade_info["confidence"],
                    "event_slug": trade_info["event_slug"],
                    "order_type": trade_info["order_type"],
                    "strategy": trade_info.get("strategy", "score"),
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
                self._failed_cids[cid] = time.time()
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
                    "strategy": trade_info.get("strategy", "score"),
                    "status": "rejected",
                    "error": error_msg,
                })
                return {"success": False, "error": error_msg}

        except Exception as e:
            error = str(e)
            print(f"[AutoTrader] ❌ Error ejecutando trade: {error}", flush=True)
            self._failed_cids[cid] = time.time()
            return {"success": False, "error": error}

    async def _get_token_id(self, condition_id: str, direction: str) -> Optional[tuple]:
        """Obtener (token_id, outcome_label) del outcome correcto via CLOB API.
        Retorna tupla (token_id, outcome) o None si no se encuentra.
        """
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{CLOB_HOST}/markets/{condition_id}")
                if resp.status_code != 200:
                    print(f"[AutoTrader] _get_token_id: CLOB status={resp.status_code}", flush=True)
                    return None
                data = resp.json()
                tokens = data.get("tokens", [])
                question = data.get("question", "")

                # Log diagnóstico: ver todos los tokens disponibles
                print(f"[AutoTrader] _get_token_id: direction={direction} question='{question[:80]}'", flush=True)
                for i, tok in enumerate(tokens):
                    print(f"[AutoTrader]   token[{i}]: outcome='{tok.get('outcome','')}' "
                          f"price={tok.get('price','?')} token_id={tok.get('token_id','')[:16]}...", flush=True)

                target = "Up" if direction == "up" else "Down"

                # Paso 1: Buscar match exacto "Up" o "Down"
                for token in tokens:
                    outcome = token.get("outcome", "")
                    if outcome.lower() == target.lower():
                        print(f"[AutoTrader]   → SELECCIONADO: '{outcome}' (match directo)", flush=True)
                        return (token.get("token_id", ""), outcome)

                # Paso 2: Mercados Yes/No — depende del contexto de la pregunta
                # Si la pregunta contiene "up or down", Yes=Up y No=Down
                # Si la pregunta es solo "up", Yes=Up, No=NOT-Up
                # Si la pregunta es solo "down", Yes=Down, No=NOT-Down
                q_lower = question.lower()
                for token in tokens:
                    outcome = token.get("outcome", "")
                    if outcome not in ("Yes", "No"):
                        continue
                    # Mercado "Up or Down": Yes = primer outcome listado
                    if "up or down" in q_lower or "up/down" in q_lower:
                        # En estos mercados, tokens son Up/Down no Yes/No
                        # Si llegamos aquí, es un caso raro
                        continue
                    # Mercado "Will X go up?": Yes=Up, No=Down
                    if direction == "up" and outcome == "Yes":
                        print(f"[AutoTrader]   → SELECCIONADO: 'Yes' (up→Yes)", flush=True)
                        return (token.get("token_id", ""), outcome)
                    if direction == "down" and outcome == "Yes" and "down" in q_lower:
                        print(f"[AutoTrader]   → SELECCIONADO: 'Yes' (down question→Yes)", flush=True)
                        return (token.get("token_id", ""), outcome)
                    if direction == "down" and outcome == "No" and "up" in q_lower and "down" not in q_lower:
                        print(f"[AutoTrader]   → SELECCIONADO: 'No' (up question→No=down)", flush=True)
                        return (token.get("token_id", ""), outcome)

                print(f"[AutoTrader]   → NO MATCH para direction={direction}", flush=True)
        except Exception as e:
            print(f"[AutoTrader] Error obteniendo token_id: {e}", flush=True)
        return None

    # ── Maker Order Execution ─────────────────────────────────────────

    async def _execute_maker_order(self, trade_info: dict) -> dict:
        """Ejecutar una orden maker (post-only GTC) en Polymarket CLOB.
        La orden se coloca POR DEBAJO del ask actual para que descanse en el book.
        No paga taker fee — recibe maker rebate.
        """
        cid = trade_info["condition_id"]
        direction = trade_info["direction"]
        bet_size = trade_info["bet_size"]
        poly_odds = trade_info["poly_odds"]
        cfg = self._config

        # Verificar límite de órdenes maker abiertas
        if len(self._open_maker_orders) >= cfg["maker_max_open_orders"]:
            return {"success": False, "error": f"Max maker orders alcanzado ({cfg['maker_max_open_orders']})"}

        try:
            token_result = await self._get_token_id(cid, direction)
            if not token_result:
                return {"success": False, "error": f"No se encontró token_id para {direction}"}
            token_id, selected_outcome = token_result

            # Precio maker = ask actual - spread_offset
            spread_offset = cfg["maker_spread_offset"]
            maker_price = round(max(poly_odds - spread_offset, 0.01), 2)
            if maker_price <= 0 or maker_price >= 1:
                return {"success": False, "error": f"Precio maker inválido: {maker_price}"}

            # Calcular shares
            from decimal import Decimal, ROUND_DOWN
            MIN_CLOB_SHARES = Decimal('5')
            d_price = Decimal(str(maker_price))
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
                return {"success": False, "error": "No se pudo calcular shares válidas"}

            print(f"[AutoTrader] MAKER Order: price=${maker_price} (ask=${poly_odds} - offset=${spread_offset}) "
                  f"shares={shares} ${bet_size}", flush=True)

            from py_clob_client.clob_types import OrderArgs, OrderType
            order_args = OrderArgs(
                price=maker_price,
                size=shares,
                side="BUY",
                token_id=token_id,
            )

            loop = asyncio.get_running_loop()
            signed_order = await loop.run_in_executor(None, self._client.create_order, order_args)

            # Post con GTC — en py-clob-client, post_order acepta OrderType
            # Para post-only, el CLOB rechaza si cruzaría el spread
            resp = await loop.run_in_executor(
                None, self._client.post_order, signed_order, OrderType.GTC
            )

            print(f"[AutoTrader] MAKER post_order response: {resp}", flush=True)

            # Parsear respuesta
            order_id = ""
            resp_status = ""
            success = False
            error_msg = ""
            if isinstance(resp, dict):
                order_id = resp.get("orderID", resp.get("order_id", "")) or ""
                resp_status = resp.get("status", "")
                success = bool(order_id)
                if not success:
                    error_msg = resp.get("errorMsg", resp.get("error", str(resp)))
            elif hasattr(resp, "orderID"):
                order_id = getattr(resp, "orderID", "") or ""
                resp_status = getattr(resp, "status", "")
                success = bool(order_id)
                error_msg = getattr(resp, "errorMsg", "")

            if success and order_id:
                # Registrar orden maker abierta — NO esperar fill inmediato
                maker_record = {
                    "order_id": order_id,
                    "condition_id": cid,
                    "coin": trade_info["coin"],
                    "direction": direction,
                    "side": "BUY",
                    "price": maker_price,
                    "size_usd": bet_size,
                    "shares": shares,
                    "token_id": token_id,
                    "edge_pct": trade_info["edge_pct"],
                    "confidence": trade_info["confidence"],
                    "event_slug": trade_info["event_slug"],
                    "order_type": "maker",
                    "strategy": trade_info.get("strategy", "score"),
                    "status": resp_status.lower(),  # "live" = en el book
                    "created_ts": time.time(),
                    "poly_odds_at_signal": poly_odds,
                }
                self._open_maker_orders[order_id] = maker_record
                self._maker_stats["placed"] += 1
                self._last_trade_time = time.time()

                print(f"[AutoTrader] ✅ MAKER ORDER PLACED: {trade_info['coin']} "
                      f"{direction.upper()} ${bet_size} @ ${maker_price:.2f} "
                      f"(status={resp_status}) order={order_id[:16]}...", flush=True)
                return {"success": True, "order_id": order_id, "status": resp_status,
                        "maker": True, "price": maker_price}
            else:
                print(f"[AutoTrader] ❌ MAKER order rechazada: {error_msg}", flush=True)
                self._failed_cids[cid] = time.time()
                return {"success": False, "error": error_msg}

        except Exception as e:
            print(f"[AutoTrader] ❌ Error ejecutando maker order: {e}", flush=True)
            self._failed_cids[cid] = time.time()
            return {"success": False, "error": str(e)}

    async def _execute_hybrid_order(self, trade_info: dict) -> dict:
        """Modo hybrid: si score alto → maker direccional, si bajo → maker ambos lados."""
        score_details = trade_info.get("score_details", {})
        score = score_details.get("score", 0) if isinstance(score_details, dict) else 0
        threshold = self._config.get("hybrid_score_threshold", 0.50)

        if score >= threshold:
            # Score alto: sesgo direccional como maker
            print(f"[AutoTrader] HYBRID: score={score:.2f} >= {threshold} → MAKER direccional", flush=True)
            return await self._execute_maker_order(trade_info)
        else:
            # Score bajo: solo maker direccional pero con spread más amplio
            print(f"[AutoTrader] HYBRID: score={score:.2f} < {threshold} → MAKER conservador (spread amplio)", flush=True)
            # Duplicar el spread offset para ser más conservador
            orig_offset = self._config["maker_spread_offset"]
            self._config["maker_spread_offset"] = round(orig_offset * 2, 2)
            result = await self._execute_maker_order(trade_info)
            self._config["maker_spread_offset"] = orig_offset  # Restaurar
            return result

    async def manage_maker_orders(self):
        """Gestionar órdenes maker abiertas: verificar fills, re-quotear, cancelar.
        Llamado cada 10s desde el loop principal.
        """
        if not self._client or not self._open_maker_orders:
            return

        now = time.time()
        to_remove = []
        loop = asyncio.get_running_loop()

        for order_id, order in list(self._open_maker_orders.items()):
            try:
                # Consultar estado de la orden
                order_info = await loop.run_in_executor(
                    None, self._client.get_order, order_id
                )
                current_status = ""
                if isinstance(order_info, dict):
                    current_status = order_info.get("status", "").lower()
                elif hasattr(order_info, "status"):
                    current_status = getattr(order_info, "status", "").lower()

                # ── MATCHED: la orden se llenó ──
                if current_status == "matched":
                    self._maker_stats["filled"] += 1
                    order["status"] = "filled"
                    # Registrar como trade exitoso
                    trade_record = {
                        "condition_id": order["condition_id"],
                        "order_id": order_id,
                        "coin": order["coin"],
                        "direction": order["direction"],
                        "side": "BUY",
                        "price": order["price"],
                        "size_usd": order["size_usd"],
                        "shares": order["shares"],
                        "token_id": order["token_id"],
                        "edge_pct": order["edge_pct"],
                        "confidence": order["confidence"],
                        "event_slug": order["event_slug"],
                        "order_type": "maker",
                        "strategy": order.get("strategy", "score"),
                        "status": "filled",
                        "created_ts": order["created_ts"],
                    }
                    await self.db.record_autotrade(trade_record)
                    self._trades_today.append(trade_record)
                    self._open_positions[order["condition_id"]] = trade_record
                    to_remove.append(order_id)
                    print(f"[AutoTrader] ✅ MAKER FILLED: {order['coin']} "
                          f"{order['direction'].upper()} @ ${order['price']:.2f} "
                          f"order={order_id[:16]}...", flush=True)

                # ── CANCELLED/EXPIRED: ya no existe ──
                elif current_status in ("cancelled", "expired", ""):
                    to_remove.append(order_id)
                    self._maker_stats["cancelled"] += 1

                # ── LIVE: aún en el book ──
                elif current_status == "live":
                    age = now - order["created_ts"]

                    # Timeout: cancelar si muy vieja
                    if age > self._config["maker_fill_timeout_sec"]:
                        try:
                            await loop.run_in_executor(None, self._client.cancel, order_id)
                            self._maker_stats["cancelled"] += 1
                            print(f"[AutoTrader] ⏰ MAKER TIMEOUT: cancelada {order['coin']} "
                                  f"({age:.0f}s > {self._config['maker_fill_timeout_sec']}s)",
                                  flush=True)
                        except Exception as cancel_err:
                            print(f"[AutoTrader] Error cancelando maker: {cancel_err}", flush=True)
                        to_remove.append(order_id)

                    # Re-quote: si el precio se movió mucho, cancelar y re-poner
                    # (solo si la orden tiene menos de la mitad del timeout)
                    elif age < self._config["maker_fill_timeout_sec"] / 2:
                        try:
                            import httpx
                            async with httpx.AsyncClient(timeout=5) as client:
                                resp = await client.get(
                                    f"https://clob.polymarket.com/markets/{order['condition_id']}")
                                if resp.status_code == 200:
                                    mdata = resp.json()
                                    if mdata.get("closed"):
                                        # Mercado cerró — cancelar orden
                                        await loop.run_in_executor(
                                            None, self._client.cancel, order_id)
                                        to_remove.append(order_id)
                                        continue
                                    tokens = mdata.get("tokens", [])
                                    for tok in tokens:
                                        outcome = tok.get("outcome", "").lower()
                                        if outcome == order["direction"]:
                                            new_ask = float(tok.get("price", 0))
                                            price_diff = abs(new_ask - order["poly_odds_at_signal"])
                                            if price_diff >= self._config["maker_requote_threshold"]:
                                                # Cancelar y re-quotear
                                                await loop.run_in_executor(
                                                    None, self._client.cancel, order_id)
                                                self._maker_stats["requoted"] += 1
                                                to_remove.append(order_id)
                                                print(f"[AutoTrader] 🔄 REQUOTE: {order['coin']} "
                                                      f"precio movió ${price_diff:.2f}", flush=True)
                                            break
                        except Exception:
                            pass  # No es crítico

            except Exception as e:
                print(f"[AutoTrader] Error gestionando maker {order_id[:16]}: {e}", flush=True)

        for oid in to_remove:
            self._open_maker_orders.pop(oid, None)

    # ── Proceso de señales (llamado desde main.py) ────────────────────

    async def process_signals(self, signals: list[dict]):
        """Evaluar y ejecutar señales. Llamado cada 5s desde loop rápido."""
        if not self._enabled or not self._client or not self._initialized:
            return
        # Evitar evaluación concurrente (el loop rápido puede llamar mientras execute_trade espera)
        if self._processing_signals:
            return
        self._processing_signals = True
        try:
            await self._process_signals_inner(signals)
        finally:
            self._processing_signals = False

    async def _process_signals_inner(self, signals: list[dict]):
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
        # Correr si hay al menos una feature de riesgo activa
        has_risk = cfg.get("stop_loss_enabled") or cfg.get("ee_take_profit_enabled") or cfg.get("sniper_tp_enabled")
        if not has_risk or not self._open_positions or not self._client:
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
                            if (direction == "up" and outcome == "up") or \
                               (direction == "down" and outcome == "down"):
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
                        strategy = trade.get("strategy", "score")

                        # 1. Stop-Loss: vender si pérdida > X%
                        if cfg["stop_loss_pct"] > 0 and pnl_pct <= -cfg["stop_loss_pct"]:
                            sell_reason = f"STOP-LOSS ({pnl_pct:.1f}% <= -{cfg['stop_loss_pct']}%)"

                        # 2. Take-Profit: vender si ganancia > X%
                        # Cada estrategia tiene su propio TP configurable
                        if not sell_reason:
                            if strategy == "sniper" and cfg.get("sniper_tp_enabled") and cfg.get("sniper_tp_pct", 0) > 0:
                                if pnl_pct >= cfg["sniper_tp_pct"]:
                                    sell_reason = f"TP-SNIPER ({pnl_pct:.1f}% >= +{cfg['sniper_tp_pct']}%)"
                            elif strategy == "early_entry" and cfg.get("ee_take_profit_enabled") and cfg.get("ee_take_profit_pct", 0) > 0:
                                if pnl_pct >= cfg["ee_take_profit_pct"]:
                                    sell_reason = f"TP-EARLY-ENTRY ({pnl_pct:.1f}% >= +{cfg['ee_take_profit_pct']}%)"
                            elif cfg["take_profit_pct"] > 0 and pnl_pct >= cfg["take_profit_pct"]:
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

            # Slippage negativo para asegurar que el SELL se ejecute
            strategy = trade.get("strategy", "score")
            if strategy == "sniper":
                sell_slip = self._config.get("sniper_slippage_pct", 5.0) / 100
            else:
                sell_slip = self._config.get("slippage_max_pct", 3.0) / 100
            sell_price = max(round(current_price - sell_slip, 2), 0.01)

            order_args = OrderArgs(
                price=sell_price,
                size=shares,
                side="SELL",
                token_id=token_id,
            )

            loop = asyncio.get_running_loop()
            signed_order = await loop.run_in_executor(None, self._client.create_order, order_args)
            resp = await loop.run_in_executor(None, self._client.post_order, signed_order, OrderType.FOK)

            # Verificar que la orden SELL se ejecutó
            resp_data = resp if isinstance(resp, dict) else resp.__dict__ if hasattr(resp, '__dict__') else {"raw": str(resp)}
            sell_filled = resp_data.get("success", False) or \
                          str(resp_data.get("status", "")).lower() == "matched"

            if not sell_filled:
                error_msg = resp_data.get("errorMsg", resp_data.get("error", str(resp)[:200]))
                print(f"[AutoTrader] ⚠️ SELL FOK no ejecutado {cid}: {error_msg} — posición sigue abierta", flush=True)
                return  # NO remover de _open_positions, se reintentará

            # Calcular PnL con precio real de entrada
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
        """Resolver trades abiertos y calcular PnL real.
        Usa CLOB API primero; si falla, usa Gamma API como fallback.
        Posiciones stale (>30 min) se resuelven forzadamente via Gamma.
        """
        if not self._open_positions:
            return

        try:
            import httpx
            now = datetime.now(timezone.utc)
            now_ts = time.time()
            resolved = 0
            GAMMA_API = getattr(config, "GAMMA_API_URL", "https://gamma-api.polymarket.com")
            STALE_THRESHOLD_SEC = 1800  # 30 minutos — cualquier mercado crypto ya cerró

            async with httpx.AsyncClient(timeout=10) as client:
                for cid, trade in list(self._open_positions.items()):
                    try:
                        winning_outcome = None
                        source = ""

                        # ── Intento 1: CLOB API ──
                        try:
                            resp = await client.get(f"{CLOB_HOST}/markets/{cid}")
                            if resp.status_code == 200:
                                data = resp.json()
                                if data.get("closed"):
                                    tokens = data.get("tokens", [])
                                    for token in tokens:
                                        if token.get("winner") is True:
                                            winning_outcome = token.get("outcome", "")
                                            source = "CLOB"
                                            break
                                        if float(token.get("price", 0)) >= 0.95:
                                            winning_outcome = token.get("outcome", "")
                                            source = "CLOB-price"
                                            break
                        except Exception as clob_err:
                            print(f"[AutoTrader] CLOB error para {cid[:16]}: {clob_err}", flush=True)

                        # ── Intento 2: Gamma API (fallback por event_slug) ──
                        if not winning_outcome:
                            event_slug = trade.get("event_slug", "")
                            if event_slug:
                                try:
                                    resp2 = await client.get(
                                        f"{GAMMA_API}/events",
                                        params={"slug": event_slug},
                                    )
                                    if resp2.status_code == 200:
                                        events = resp2.json()
                                        if events and len(events) > 0:
                                            ev = events[0]
                                            if ev.get("closed") or not ev.get("active", True):
                                                for m in ev.get("markets", []):
                                                    if m.get("conditionId") == cid or not cid:
                                                        # Buscar ganador en tokens del mercado
                                                        for tok in m.get("tokens", m.get("outcomes", [])):
                                                            if isinstance(tok, dict):
                                                                if tok.get("winner") is True:
                                                                    winning_outcome = tok.get("outcome", "")
                                                                    source = "Gamma"
                                                                    break
                                                                if float(tok.get("price", 0)) >= 0.95:
                                                                    winning_outcome = tok.get("outcome", "")
                                                                    source = "Gamma-price"
                                                                    break
                                                        if winning_outcome:
                                                            break
                                                # Fallback: si el evento cerró pero no hay tokens con winner,
                                                # intentar deducir del título/outcome_prices
                                                if not winning_outcome and (ev.get("closed") or not ev.get("active", True)):
                                                    for m in ev.get("markets", []):
                                                        op = m.get("outcomePrices")
                                                        if op:
                                                            try:
                                                                # outcomePrices es string JSON: "[0.95, 0.05]"
                                                                import json
                                                                prices = json.loads(op) if isinstance(op, str) else op
                                                                outcomes = m.get("outcomes", [])
                                                                if isinstance(outcomes, str):
                                                                    outcomes = json.loads(outcomes)
                                                                for i, p in enumerate(prices):
                                                                    if float(p) >= 0.90 and i < len(outcomes):
                                                                        winning_outcome = outcomes[i]
                                                                        source = "Gamma-outcomePrices"
                                                                        break
                                                            except Exception:
                                                                pass
                                                        if winning_outcome:
                                                            break
                                except Exception as gamma_err:
                                    print(f"[AutoTrader] Gamma error para {event_slug}: {gamma_err}", flush=True)

                        # ── Intento 3: posición stale — forzar resolución ──
                        trade_age = now_ts - trade.get("created_ts", now_ts)
                        if not winning_outcome and trade_age > STALE_THRESHOLD_SEC:
                            print(f"[AutoTrader] ⚠️ Posición stale ({trade_age:.0f}s > {STALE_THRESHOLD_SEC}s), "
                                  f"forzando resolución como LOSS: {trade.get('coin','')} {trade.get('direction','')}", flush=True)
                            winning_outcome = "__STALE__"
                            source = "stale-timeout"

                        if not winning_outcome:
                            continue

                        # Determinar si ganamos
                        direction = trade.get("direction", "")
                        if winning_outcome == "__STALE__":
                            # Posición stale: marcar como loss (no pudimos verificar)
                            won = False
                        else:
                            won = (direction == "up" and winning_outcome.lower() == "up") or \
                                  (direction == "down" and winning_outcome.lower() == "down")
                        print(f"[AutoTrader] Resolve ({source}): direction={direction} winner='{winning_outcome}' → {'WIN' if won else 'LOSS'}", flush=True)

                        shares = trade.get("shares", 0)
                        size_usd = trade.get("size_usd", 0)
                        if won:
                            # Cada share ganadora paga $1.00 — usar shares reales
                            if shares > 0:
                                pnl = round(shares - size_usd, 2)
                            else:
                                # Fallback si no hay shares guardadas
                                entry_p = max(trade.get("price", 0.5), 0.01)
                                pnl = round(size_usd * ((1.0 / entry_p) - 1), 2)
                            result = "win"
                        else:
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
                print(f"[AutoTrader] 📊 {resolved} trades resueltos en este ciclo", flush=True)

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

    # ── Auto-Claim (Builder Relayer) ──────────────────────────────────

    def _init_redeem_service(self):
        """Inicializar PolyWeb3Service para redeem automático."""
        try:
            from py_builder_relayer_client.client import RelayClient
            from py_builder_signing_sdk.config import BuilderConfig
            from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
            from poly_web3 import RELAYER_URL, PolyWeb3Service

            pk = self._config.get("private_key", "")
            if pk and not pk.startswith("0x"):
                pk = "0x" + pk

            builder_key = config.BUILDER_KEY
            builder_secret = config.BUILDER_SECRET
            builder_passphrase = config.BUILDER_PASSPHRASE

            if not all([builder_key, builder_secret, builder_passphrase]):
                print("[AutoTrader] Auto-claim: Builder keys no configuradas", flush=True)
                return

            relayer_client = RelayClient(
                RELAYER_URL,
                CHAIN_ID,
                pk,
                BuilderConfig(
                    local_builder_creds=BuilderApiKeyCreds(
                        key=builder_key,
                        secret=builder_secret,
                        passphrase=builder_passphrase,
                    )
                ),
            )

            self._poly_web3_service = PolyWeb3Service(
                clob_client=self._client,
                relayer_client=relayer_client,
                rpc_url="https://polygon-bor-rpc.publicnode.com",
            )
            print("[AutoTrader] ✅ Auto-claim inicializado (Builder Relayer)", flush=True)

        except ImportError as e:
            print(f"[AutoTrader] Auto-claim no disponible (instalar poly-web3): {e}", flush=True)
            self._poly_web3_service = None
        except Exception as e:
            print(f"[AutoTrader] Error inicializando auto-claim: {e}", flush=True)
            self._poly_web3_service = None

    async def auto_claim(self):
        """Auto-claim posiciones ganadoras resueltas via Builder Relayer.
        Se ejecuta cada 2 minutos máximo para no saturar el relayer.
        """
        now = time.time()
        # No intentar claim más de cada 120 segundos
        if now - self._last_claim_time < 120:
            return

        # Verificar que hay Builder keys configuradas
        if not all([config.BUILDER_KEY, config.BUILDER_SECRET, config.BUILDER_PASSPHRASE]):
            return

        if not self._client:
            return

        try:
            # Lazy init del servicio de redeem
            if not self._poly_web3_service:
                self._init_redeem_service()
            if not self._poly_web3_service:
                return

            self._last_claim_time = now

            # redeem_all es síncrono — ejecutar en executor
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, self._poly_web3_service.redeem_all, 10
            )

            if result:
                claimed = [r for r in result if r is not None]
                failed = [r for r in result if r is None]
                if claimed:
                    print(f"[AutoTrader] 💰 AUTO-CLAIM: {len(claimed)} posiciones reclamadas exitosamente", flush=True)
                    for c in claimed:
                        print(f"[AutoTrader]   → Claim: {c}", flush=True)
                if failed:
                    print(f"[AutoTrader] ⚠️ AUTO-CLAIM: {len(failed)} fallaron, se reintentarán", flush=True)
            # Si result es vacío o None, no hay nada que reclamar (normal)

        except Exception as e:
            print(f"[AutoTrader] Auto-claim error: {e}", flush=True)
