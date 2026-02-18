"""Motor de autotrading para alertas de insider — copy-trading de smart money.

Flujo:
1. Recibe AlertCandidate + Trade cuando se dispara una alerta
2. Filtra por configuración (score, odds, hit_rate, límites diarios)
3. Busca token_id del outcome apostado por el insider
4. Verifica profundidad del orderbook (price impact)
5. Coloca orden BUY via py-clob-client (DCA si habilitado)
6. Registra trade en tabla alert_autotrades
7. Take Profit: vende automáticamente si el precio sube >= X% (configurable)
8. Se resuelve cuando el mercado cierra o se ejecuta take profit

v10.0: Orderbook depth check, DCA entry, volume spike detection,
       smart watchlist boost, funding chain analysis, Kelly calibrado,
       category-specific scoring, improved correlation filter.

Credenciales: usa SOLO wallet propia (aat_). No comparte wallet con Crypto Arb.
La config de trading es independiente con prefijo "aat_".
"""
import asyncio
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog
from src import config

logger = structlog.get_logger()

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
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
        self._trailing_highs: dict[str, float] = {}  # cid -> max_price visto
        self._partial_sold: set[str] = set()  # cid que ya hicieron partial TP
        self._peak_balance: float = 0.0  # Para max drawdown tracking
        self._drawdown_paused = False
        self._initialized = False
        # v10: Volume spike tracker — {market_id: [{ts, volume, side}]}
        self._volume_tracker: dict[str, list] = defaultdict(list)
        # v10: Smart watchlist cache (se refresca cada 30 min)
        self._smart_watchlist: set[str] = set()
        self._watchlist_updated: float = 0
        # v10: Category win rates cache
        self._category_win_rates: dict[str, float] = {}
        self._category_wr_updated: float = 0
        # v10: Backtest calibration data
        self._calibrated_win_rate: float = 0.55  # default
        self._calibrated_at: float = 0

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
                # Fase 4 — Kelly Criterion
                "aat_kelly_enabled", "aat_kelly_base_fraction",
                # Fase 4 — Trailing Stop Loss
                "aat_trailing_stop_enabled", "aat_trailing_stop_pct",
                # Fase 4 — Partial Profit Taking
                "aat_partial_tp_enabled", "aat_partial_tp_pct", "aat_partial_tp_fraction",
                # Fase 4 — Auto Exit on insider SELL
                "aat_auto_exit_on_sell",
                # Fase 4 — Filtro liquidez mínima
                "aat_min_market_liquidity",
                # Fase 4 — Auto-scaling por PNL
                "aat_auto_scale_enabled", "aat_auto_scale_win_boost", "aat_auto_scale_loss_reduce",
                # Fase 4 — Diversificación forzada
                "aat_max_category_exposure",
                # Fase 4 — Max drawdown
                "aat_max_drawdown",
                # Credenciales propias del alert autotrader (NO usa las de Crypto Arb)
                "aat_api_key", "aat_api_secret", "aat_private_key", "aat_passphrase",
                "aat_funder_address",
                # v10: Nuevas features
                "aat_orderbook_check_enabled", "aat_max_price_impact_pct",
                "aat_dca_enabled", "aat_dca_splits", "aat_dca_interval_sec",
                "aat_volume_spike_boost",
                "aat_smart_watchlist_boost",
                "aat_funding_chain_boost",
                "aat_category_scoring_enabled",
                # Copy Trade automático
                "aat_copy_trade_enabled", "aat_copy_trade_bet_size",
                "aat_copy_trade_max_positions", "aat_copy_trade_max_daily",
            ])
            pk = raw.get("aat_private_key", "")
            if pk and not pk.startswith("0x"):
                pk = "0x" + pk
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
                # Kelly Criterion
                "kelly_enabled": raw.get("aat_kelly_enabled") == "true",
                "kelly_base_fraction": float(raw.get("aat_kelly_base_fraction", 0.25)),
                # Trailing Stop Loss
                "trailing_stop_enabled": raw.get("aat_trailing_stop_enabled") == "true",
                "trailing_stop_pct": float(raw.get("aat_trailing_stop_pct", 15)),
                # Partial Profit Taking
                "partial_tp_enabled": raw.get("aat_partial_tp_enabled") == "true",
                "partial_tp_pct": float(raw.get("aat_partial_tp_pct", 30)),
                "partial_tp_fraction": float(raw.get("aat_partial_tp_fraction", 50)),
                # Auto Exit on insider SELL
                "auto_exit_on_sell": raw.get("aat_auto_exit_on_sell") == "true",
                # Filtro liquidez mínima
                "min_market_liquidity": float(raw.get("aat_min_market_liquidity", 0)),
                # Auto-scaling por PNL
                "auto_scale_enabled": raw.get("aat_auto_scale_enabled") == "true",
                "auto_scale_win_boost": float(raw.get("aat_auto_scale_win_boost", 10)),
                "auto_scale_loss_reduce": float(raw.get("aat_auto_scale_loss_reduce", 20)),
                # Diversificación forzada
                "max_category_exposure": int(raw.get("aat_max_category_exposure", 0)),
                # Max drawdown
                "max_drawdown": float(raw.get("aat_max_drawdown", 0)),
                "api_key": raw.get("aat_api_key", ""),
                "api_secret": raw.get("aat_api_secret", ""),
                "private_key": pk,
                "passphrase": raw.get("aat_passphrase", ""),
                "funder_address": raw.get("aat_funder_address", ""),
                "has_own_wallet": bool(raw.get("aat_private_key")),
                # v10: Nuevas features
                "orderbook_check_enabled": raw.get("aat_orderbook_check_enabled") == "true",
                "max_price_impact_pct": float(raw.get("aat_max_price_impact_pct", 3.0)),
                "dca_enabled": raw.get("aat_dca_enabled") == "true",
                "dca_splits": int(raw.get("aat_dca_splits", 2)),
                "dca_interval_sec": int(raw.get("aat_dca_interval_sec", 30)),
                "volume_spike_boost": int(raw.get("aat_volume_spike_boost", 3)),
                "smart_watchlist_boost": int(raw.get("aat_smart_watchlist_boost", 3)),
                "funding_chain_boost": int(raw.get("aat_funding_chain_boost", 2)),
                "category_scoring_enabled": raw.get("aat_category_scoring_enabled") == "true",
                # Copy Trade automático
                "copy_trade_enabled": raw.get("aat_copy_trade_enabled") == "true",
                "copy_trade_bet_size": float(raw.get("aat_copy_trade_bet_size", 10)),
                "copy_trade_max_positions": int(raw.get("aat_copy_trade_max_positions", 3)),
                "copy_trade_max_daily": int(raw.get("aat_copy_trade_max_daily", 10)),
            }
            self._enabled = self._config["enabled"]

            needs_client = self._enabled or self._config.get("copy_trade_enabled")
            if needs_client and self._config["api_key"] and self._config["private_key"]:
                self._init_clob_client()
            else:
                self._client = None

            await self._load_today_trades()
            await self._load_open_positions()

            self._initialized = True
            status = "ACTIVADO" if self._enabled and self._client else "DESACTIVADO"
            ct_status = "ON" if self._config.get("copy_trade_enabled") and self._client else "OFF"
            reason = ""
            if needs_client and not self._client:
                reason = " (sin credenciales — configura wallet en Alert Trading)"
            print(f"[AlertTrader] {status}{reason} | CopyTrade={ct_status} "
                  f"bet=${self._config['bet_size']} ct_bet=${self._config.get('copy_trade_bet_size', 10)}",
                  flush=True)
        except Exception as e:
            print(f"[AlertTrader] Error inicializando: {e}", flush=True)
            self._enabled = False

    def _init_clob_client(self):
        """Crear cliente CLOB con credenciales propias."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=self._config["api_key"],
                api_secret=self._config["api_secret"],
                api_passphrase=self._config["passphrase"],
            )
            funder = self._config.get("funder_address", "") or None
            self._client = ClobClient(
                CLOB_HOST,
                key=self._config["private_key"],
                chain_id=CHAIN_ID,
                signature_type=2,
                funder=funder,
                creds=creds,
            )
            print(f"[AlertTrader] Cliente CLOB inicializado OK (funder={'set: '+funder[:10]+'...' if funder else 'NOT SET - orders will fail!'})", flush=True)
            if not funder:
                print("[AlertTrader] ⚠️ FUNDER ADDRESS no configurada. Ve a polymarket.com/settings, copia tu Proxy Wallet Address.", flush=True)
            else:
                self._check_allowances()
        except ImportError:
            print("[AlertTrader] ERROR: py-clob-client no instalado", flush=True)
            self._client = None
        except Exception as e:
            print(f"[AlertTrader] Error creando cliente CLOB: {e}", flush=True)
            self._client = None

    def _check_allowances(self):
        """Verificar token allowances para trading."""
        if not self._client:
            return
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams
            params = BalanceAllowanceParams(asset_type="COLLATERAL")
            bal = self._client.get_balance_allowance(params)
            balance = bal.get("balance", "?") if isinstance(bal, dict) else getattr(bal, "balance", "?")
            allowance = bal.get("allowance", "?") if isinstance(bal, dict) else getattr(bal, "allowance", "?")
            print(f"[AlertTrader] Allowance COLLATERAL: balance={balance} allowance={allowance}", flush=True)
            try:
                allow_val = float(str(allowance))
                if allow_val == 0:
                    print("[AlertTrader] ⚠️ ALLOWANCE = 0. Ve a polymarket.com y firma 'Enable Trading' + 'Approve Tokens'.", flush=True)
            except (ValueError, TypeError):
                pass
        except Exception as e:
            print(f"[AlertTrader] Allowance check error: {e}", flush=True)

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
        v10: Incluye smart watchlist boost, volume spike, funding chain, category scoring.
        """
        if not self._enabled or not self._client or not self._initialized:
            return None

        cfg = self._config

        # v10: Refrescar caches periódicamente
        await self._refresh_smart_watchlist()
        await self._refresh_category_win_rates()
        await self._calibrate_kelly()

        # ── Score ajustado con v10 boosts ──
        adjusted_score = candidate.score
        extra_triggers = []

        # v10: Smart Watchlist boost
        if self.is_smart_wallet(trade.wallet_address):
            boost = cfg.get("smart_watchlist_boost", 3)
            adjusted_score += boost
            extra_triggers.append(f"⭐ Smart Watchlist (+{boost})")

        # v10: Volume spike boost
        spike_ratio = self.get_volume_spike_ratio(trade.market_id)
        direction_bias = self.get_volume_direction_bias(trade.market_id)
        if spike_ratio >= 3.0 and direction_bias >= 80:
            boost = cfg.get("volume_spike_boost", 3)
            adjusted_score += boost
            extra_triggers.append(f"📊 Vol spike {spike_ratio:.1f}x ({direction_bias:.0f}% dir)")

        # v10: Funding chain boost
        funding_boost = await self._check_funding_chain(trade.wallet_address)
        if funding_boost > 0:
            adjusted_score += funding_boost
            extra_triggers.append(f"🔗 Smart funder (+{funding_boost})")

        # Filtro: score mínimo (usando score ajustado)
        if adjusted_score < cfg["min_score"]:
            return None

        # Filtro: require smart money (wallet en watchlist o tiene triggers smart)
        if cfg["require_smart_money"]:
            is_smart = self.is_smart_wallet(trade.wallet_address) or \
                        any("Smart Money" in t or "Ganador probado" in t for t in candidate.triggers)
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

        # v10: Improved Correlation Filter — usa entidades en vez de solo palabras
        if config.FEATURE_CORRELATION_FILTER and self._open_positions:
            market_q = trade.market_question or ""
            for pos_cid, pos in self._open_positions.items():
                pos_q = pos.get("market_question", "") or ""
                if self._markets_correlated(market_q, pos_q):
                    return None  # Mercado correlacionado a posición abierta

        # Cooldown general entre trades
        now = time.time()
        if now - self._last_trade_time < 10:  # 10 seg mínimo entre trades
            return None

        # Filtro: max drawdown — pausar si drawdown excede límite
        if cfg.get("max_drawdown", 0) > 0 and self._drawdown_paused:
            return None

        # Filtro: diversificación forzada por categoría
        max_cat = cfg.get("max_category_exposure", 0)
        if max_cat > 0:
            cat = (trade.market_category or "").lower()
            if cat:
                cat_count = sum(1 for p in self._open_positions.values()
                                if (p.get("category", "") or "").lower() == cat)
                if cat_count >= max_cat:
                    return None

        # ── Calcular bet_size (base o Kelly) ──
        bet_size = cfg["bet_size"]

        # Kelly Criterion: sizing dinámico basado en probabilidad CALIBRADA
        if cfg.get("kelly_enabled"):
            bet_size = self._kelly_bet_size(candidate, trade, cfg)

        # Auto-scaling por PNL: ajustar bet_size según rendimiento del día
        if cfg.get("auto_scale_enabled"):
            bet_size = self._auto_scale_bet(bet_size, cfg)

        # v10: Category-specific scaling
        cat_mult = self.get_category_multiplier(trade.market_category or "")
        bet_size = bet_size * cat_mult

        # Merge triggers
        all_triggers = list(candidate.triggers[:5]) + extra_triggers

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
            "alert_score": adjusted_score,
            "triggers": ", ".join(all_triggers),
            "wallet_hit_rate": candidate.wallet_hit_rate or 0,
            "bet_size": round(bet_size, 2),
            "category": trade.market_category or "",
            "volume_spike": round(spike_ratio, 1),
            "category_multiplier": cat_mult,
        }

    def _kelly_bet_size(self, candidate, trade, cfg: dict) -> float:
        """Calcular bet size usando Kelly Criterion con win rate CALIBRADO.
        Kelly fraction = (p * b - q) / b
        donde p = probabilidad calibrada de ganar, q = 1-p, b = odds netas
        v10: Usa win rate real del backtest en vez de score arbitrario.
        """
        base_bet = cfg["bet_size"]
        kelly_fraction = cfg.get("kelly_base_fraction", 0.25)

        # v10: Base probability = calibrated win rate (de datos reales)
        base_wr = self._calibrated_win_rate  # Calibrado cada 6h

        # Ajustar por score relativo al mínimo
        score = candidate.score
        min_score = cfg.get("min_score", 7)
        # Scores más altos que el mínimo → boost proporcional
        score_boost = min((score - min_score) / 20, 0.15)  # max +15%
        score_prob = min(base_wr + score_boost, 0.90)

        # Ajustar por hit rate de la wallet si disponible
        hr = candidate.wallet_hit_rate or 0
        if hr >= 80:
            score_prob = min(score_prob + 0.08, 0.92)
        elif hr >= 70:
            score_prob = min(score_prob + 0.04, 0.90)

        # Odds netas: cuánto ganamos por cada $1 apostado
        price = trade.price
        if price and 0 < price < 1:
            b = (1.0 / price) - 1  # Ej: precio 0.40 → ganamos $1.50 por $1
        else:
            b = 1.0

        p = score_prob
        q = 1 - p

        # Kelly = (p*b - q) / b
        kelly = (p * b - q) / b if b > 0 else 0
        kelly = max(kelly, 0)  # No apostar si Kelly negativo

        # Usar fracción de Kelly (más conservador)
        fraction = kelly * kelly_fraction
        fraction = max(0.05, min(fraction, 0.5))  # 5%-50% del bankroll base

        adjusted_bet = base_bet * (fraction / 0.25)  # Normalizado al 25%
        adjusted_bet = max(1.0, min(adjusted_bet, base_bet * 3))  # Min $1, max 3x base

        return adjusted_bet

    def _auto_scale_bet(self, bet_size: float, cfg: dict) -> float:
        """Ajustar bet_size según PNL del día: ganar más → subir, perder → bajar."""
        daily_pnl = sum(t.get("pnl", 0) for t in self._trades_today if t.get("resolved"))
        win_boost = cfg.get("auto_scale_win_boost", 10) / 100  # % a subir por cada $10 ganados
        loss_reduce = cfg.get("auto_scale_loss_reduce", 20) / 100  # % a bajar por cada $10 perdidos

        if daily_pnl > 0:
            # Escalar hacia arriba: +win_boost% por cada $10 de profit
            scale = 1 + (daily_pnl / 10) * win_boost
            scale = min(scale, 2.0)  # Max 2x
        elif daily_pnl < 0:
            # Escalar hacia abajo: -loss_reduce% por cada $10 de pérdida
            scale = 1 + (daily_pnl / 10) * loss_reduce  # daily_pnl es negativo
            scale = max(scale, 0.3)  # Min 30%
        else:
            scale = 1.0

        return bet_size * scale

    # ── v10: Orderbook Depth Check ────────────────────────────────────

    async def _check_orderbook_depth(self, token_id: str, side: str, size_usd: float) -> dict:
        """Consultar CLOB orderbook y calcular price impact antes de ejecutar.
        Retorna {ok: bool, price_impact_pct: float, best_price: float, depth_usd: float}
        """
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(f"{CLOB_HOST}/book", params={"token_id": token_id})
                if resp.status_code != 200:
                    return {"ok": True, "price_impact_pct": 0, "best_price": 0, "depth_usd": 0}
                book = resp.json()

                # Para BUY miramos los asks, para SELL los bids
                orders = book.get("asks", []) if side == "BUY" else book.get("bids", [])
                if not orders:
                    return {"ok": True, "price_impact_pct": 0, "best_price": 0, "depth_usd": 0}

                # Calcular depth y price impact
                total_depth_usd = 0
                filled_usd = 0
                worst_price = 0
                best_price = float(orders[0].get("price", 0)) if orders else 0

                for order in orders:
                    price = float(order.get("price", 0))
                    size = float(order.get("size", 0))
                    level_usd = price * size
                    total_depth_usd += level_usd
                    if filled_usd < size_usd:
                        remaining = size_usd - filled_usd
                        take = min(level_usd, remaining)
                        filled_usd += take
                        worst_price = price

                if best_price <= 0:
                    return {"ok": True, "price_impact_pct": 0, "best_price": 0, "depth_usd": total_depth_usd}

                price_impact_pct = abs(worst_price - best_price) / best_price * 100 if worst_price > 0 else 0

                return {
                    "ok": True,
                    "price_impact_pct": round(price_impact_pct, 2),
                    "best_price": best_price,
                    "depth_usd": round(total_depth_usd, 2),
                }
        except Exception as e:
            print(f"[AlertTrader] Orderbook check error: {e}", flush=True)
            return {"ok": True, "price_impact_pct": 0, "best_price": 0, "depth_usd": 0}

    # ── v10: Volume Spike Detection ───────────────────────────────────

    def track_volume(self, market_id: str, size: float, side: str):
        """Registrar volumen de un trade para detección de spikes."""
        now = time.time()
        self._volume_tracker[market_id].append({
            "ts": now, "volume": size, "side": side
        })
        # Limpiar datos viejos (>4h)
        cutoff = now - 14400
        self._volume_tracker[market_id] = [
            v for v in self._volume_tracker[market_id] if v["ts"] > cutoff
        ]

    def get_volume_spike_ratio(self, market_id: str) -> float:
        """Calcular ratio de volumen última hora vs promedio 4h.
        Retorna >1 si hay spike (ej: 5.0 = 5x el promedio).
        """
        entries = self._volume_tracker.get(market_id, [])
        if len(entries) < 3:
            return 1.0
        now = time.time()
        vol_1h = sum(e["volume"] for e in entries if e["ts"] > now - 3600)
        vol_4h = sum(e["volume"] for e in entries)
        hours_4h = min((now - entries[0]["ts"]) / 3600, 4.0) if entries else 4.0
        avg_hourly = (vol_4h / max(hours_4h, 0.5))
        if avg_hourly <= 0:
            return 1.0
        return vol_1h / avg_hourly

    def get_volume_direction_bias(self, market_id: str) -> float:
        """Porcentaje de volumen en una sola dirección en última hora.
        Retorna 0-100 (100 = 100% buy o 100% sell).
        """
        entries = self._volume_tracker.get(market_id, [])
        now = time.time()
        recent = [e for e in entries if e["ts"] > now - 3600]
        if not recent:
            return 50.0
        buy_vol = sum(e["volume"] for e in recent if e["side"] == "BUY")
        total = sum(e["volume"] for e in recent)
        if total <= 0:
            return 50.0
        return max(buy_vol / total * 100, (1 - buy_vol / total) * 100)

    # ── v10: Smart Watchlist ──────────────────────────────────────────

    async def _refresh_smart_watchlist(self):
        """Actualizar cache de wallets en watchlist (cada 30 min)."""
        now = time.time()
        if now - self._watchlist_updated < 1800 and self._smart_watchlist:
            return
        try:
            self._smart_watchlist = await self.db.get_watchlisted_wallets()
            self._watchlist_updated = now
        except Exception as e:
            print(f"[AlertTrader] Error refreshing watchlist: {e}", flush=True)

    def is_smart_wallet(self, address: str) -> bool:
        """Verificar si wallet está en la smart watchlist."""
        return address.lower() in self._smart_watchlist

    # ── v10: Category Win Rates ───────────────────────────────────────

    async def _refresh_category_win_rates(self):
        """Actualizar win rates por categoría (cada 1h)."""
        now = time.time()
        if now - self._category_wr_updated < 3600 and self._category_win_rates:
            return
        try:
            edges = await self.db.get_category_edge()
            self._category_win_rates = {}
            for e in edges:
                cat = (e.get("category") or "").lower()
                if cat and e.get("resolved", 0) >= 5:
                    self._category_win_rates[cat] = e.get("win_rate", 50)
            self._category_wr_updated = now
        except Exception:
            pass

    def get_category_multiplier(self, category: str) -> float:
        """Multiplicador de bet size según win rate de la categoría.
        Categorías con >60% WR → hasta 1.5x, <40% WR → hasta 0.5x
        """
        if not self._config.get("category_scoring_enabled"):
            return 1.0
        cat = (category or "").lower()
        wr = self._category_win_rates.get(cat)
        if wr is None:
            return 1.0
        if wr >= 70:
            return 1.5
        elif wr >= 60:
            return 1.25
        elif wr >= 50:
            return 1.0
        elif wr >= 40:
            return 0.75
        else:
            return 0.5

    # ── v10: Funding Chain Boost ──────────────────────────────────────

    async def _check_funding_chain(self, wallet_address: str) -> int:
        """Verificar si el funder de esta wallet es smart money.
        Retorna puntos extra (0 o funding_chain_boost).
        """
        boost = self._config.get("funding_chain_boost", 0)
        if not boost:
            return 0
        try:
            async with self.db._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT on_chain_funded_by FROM wallets WHERE address = $1",
                    wallet_address.lower()
                )
                if not row or not row["on_chain_funded_by"]:
                    return 0
                funder = row["on_chain_funded_by"].lower()
                # Verificar si el funder es smart money
                funder_row = await conn.fetchrow(
                    "SELECT smart_money_score, win_count, loss_count FROM wallets WHERE address = $1",
                    funder
                )
                if not funder_row:
                    return 0
                score = float(funder_row["smart_money_score"] or 0)
                wins = funder_row["win_count"] or 0
                losses = funder_row["loss_count"] or 0
                total = wins + losses
                if total >= 5 and score >= 50:
                    return boost
                if total >= 3 and wins / max(total, 1) >= 0.65:
                    return boost
        except Exception:
            pass
        return 0

    # ── v10: Kelly Calibrado con Backtest ─────────────────────────────

    async def _calibrate_kelly(self):
        """Calibrar probabilidad de win para Kelly usando datos reales.
        Se ejecuta cada 6 horas.
        """
        now = time.time()
        if now - self._calibrated_at < 21600 and self._calibrated_at > 0:
            return
        try:
            async with self.db._pool.acquire() as conn:
                # Win rate real de los alert autotrades
                row = await conn.fetchrow("""
                    SELECT COUNT(*) FILTER (WHERE result IN ('win','take_profit','trailing_stop')) as wins,
                           COUNT(*) as total
                    FROM alert_autotrades
                    WHERE resolved = TRUE AND created_at > NOW() - INTERVAL '30 days'
                """)
                if row and row["total"] and row["total"] >= 10:
                    self._calibrated_win_rate = row["wins"] / row["total"]
                    print(f"[AlertTrader] Kelly calibrado: WR={self._calibrated_win_rate:.1%} "
                          f"({row['wins']}/{row['total']} trades)", flush=True)
                else:
                    # Fallback: usar win rate de alertas simuladas
                    row2 = await conn.fetchrow("""
                        SELECT COUNT(*) FILTER (WHERE was_correct) as wins,
                               COUNT(*) as total
                        FROM alerts WHERE resolved = TRUE AND score >= 7
                          AND created_at > NOW() - INTERVAL '30 days'
                    """)
                    if row2 and row2["total"] and row2["total"] >= 20:
                        self._calibrated_win_rate = row2["wins"] / row2["total"]
            self._calibrated_at = now
        except Exception as e:
            print(f"[AlertTrader] Kelly calibration error: {e}", flush=True)

    # ── v10: Improved Correlation Filter ──────────────────────────────

    def _markets_correlated(self, q1: str, q2: str) -> bool:
        """Verificar si dos mercados están correlacionados usando entidades + categoría."""
        if not q1 or not q2:
            return False
        q1_lower = q1.lower()
        q2_lower = q2.lower()

        # Extraer entidades (palabras capitalizadas, nombres propios)
        import re
        entities_1 = set(re.findall(r'\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)*\b', q1))
        entities_2 = set(re.findall(r'\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)*\b', q2))

        # Entidades compartidas (más preciso que overlap de palabras)
        if entities_1 and entities_2:
            shared = entities_1 & entities_2
            if len(shared) >= 1 and len(shared) / max(len(entities_1 | entities_2), 1) >= 0.3:
                return True

        # Fallback: overlap de palabras significativas (>4 chars)
        stop_words = {"will", "what", "when", "does", "about", "before", "after",
                      "market", "price", "above", "below", "this", "that", "with"}
        words_1 = set(w for w in q1_lower.split() if len(w) > 4 and w not in stop_words)
        words_2 = set(w for w in q2_lower.split() if len(w) > 4 and w not in stop_words)
        if words_1 and words_2:
            overlap = len(words_1 & words_2) / max(len(words_1 | words_2), 1)
            if overlap >= 0.5:
                return True

        return False

    # ── Ejecución de órdenes ─────────────────────────────────────────

    async def execute_trade(self, trade_info: dict) -> dict:
        """Ejecutar copy-trade en Polymarket CLOB.
        v10: Incluye orderbook depth check y DCA (time-weighted entry).
        """
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
            # Copy trades bypasean este filtro — la decisión ya la tomó la wallet seguida
            cfg = self._config
            if not trade_info.get("is_copy_trade"):
                if price > cfg["max_odds"] or price < cfg["min_odds"]:
                    return {"success": False, "error": f"Precio actual {price:.2f} fuera de rango [{cfg['min_odds']}, {cfg['max_odds']}]"}

            # v10: Orderbook Depth Check — verificar price impact antes de ejecutar
            if cfg.get("orderbook_check_enabled"):
                ob_result = await self._check_orderbook_depth(token_id, "BUY", bet_size)
                max_impact = cfg.get("max_price_impact_pct", 3.0)
                if ob_result["price_impact_pct"] > max_impact:
                    return {"success": False,
                            "error": f"Price impact {ob_result['price_impact_pct']:.1f}% > max {max_impact}% "
                                     f"(depth=${ob_result['depth_usd']:.0f})"}
                if ob_result["depth_usd"] > 0 and ob_result["depth_usd"] < bet_size * 0.5:
                    # Reducir bet size si hay poca liquidez
                    old_bet = bet_size
                    bet_size = min(bet_size, ob_result["depth_usd"] * 0.4)
                    bet_size = max(bet_size, 1.0)
                    if bet_size < old_bet:
                        print(f"[AlertTrader] ⚠️ Bet reducido por liquidez: ${old_bet:.2f} → ${bet_size:.2f} "
                              f"(depth=${ob_result['depth_usd']:.0f})", flush=True)

            # v10: DCA — dividir en múltiples órdenes
            if cfg.get("dca_enabled") and cfg.get("dca_splits", 1) > 1:
                return await self._execute_dca(trade_info, token_id, price, bet_size)
            else:
                return await self._execute_single_order(trade_info, token_id, price, bet_size)

        except Exception as e:
            error = str(e)
            print(f"[AlertTrader] ❌ Error ejecutando trade: {error}", flush=True)
            return {"success": False, "error": error}

    def _calc_shares(self, bet_size: float, order_price: float) -> float:
        """Calcular shares con precisión Decimal para CLOB."""
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
        return float(d_shares)

    async def _post_and_poll_order(self, order_args, token_id: str) -> tuple:
        """Postear orden GTC y hacer polling. Retorna (success, order_id, error_msg)."""
        from py_clob_client.clob_types import OrderType
        loop = asyncio.get_running_loop()
        signed_order = await loop.run_in_executor(None, self._client.create_order, order_args)
        resp = await loop.run_in_executor(None, self._client.post_order, signed_order, OrderType.GTC)

        success = False
        order_id = ""
        error_msg = ""
        resp_status = ""

        if isinstance(resp, dict):
            order_id = resp.get("orderID", resp.get("order_id", "")) or ""
            resp_status = resp.get("status", "")
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

        # GTC 'live': polling para verificar fill
        if success and resp_status.lower() == "live" and order_id:
            print(f"[AlertTrader] ⏳ Orden GTC en orderbook (status=live), esperando fill...", flush=True)
            filled = False
            for attempt in range(12):  # 12 × 5s = 60s máximo
                await asyncio.sleep(5)
                try:
                    order_info = await loop.run_in_executor(None, self._client.get_order, order_id)
                    current_status = ""
                    if isinstance(order_info, dict):
                        current_status = order_info.get("status", "")
                    elif hasattr(order_info, "status"):
                        current_status = getattr(order_info, "status", "")
                    print(f"[AlertTrader]   polling {attempt+1}/12: status={current_status}", flush=True)
                    if current_status.lower() == "matched":
                        filled = True
                        break
                    elif current_status.lower() in ("cancelled", "expired", ""):
                        break
                except Exception as poll_err:
                    print(f"[AlertTrader]   polling error: {poll_err}", flush=True)
                    break

            if not filled:
                try:
                    await loop.run_in_executor(None, self._client.cancel, order_id)
                    print(f"[AlertTrader] ❌ Orden GTC no llenada, CANCELADA: {order_id[:16]}...", flush=True)
                except Exception:
                    pass
                success = False
                error_msg = "GTC order not filled within timeout, cancelled"
            else:
                print(f"[AlertTrader] ✅ Orden GTC llenada (matched)!", flush=True)

        return success, order_id, error_msg

    async def _execute_single_order(self, trade_info: dict, token_id: str, price: float, bet_size: float) -> dict:
        """Ejecutar una sola orden BUY."""
        cid = trade_info["condition_id"]
        outcome = trade_info["buy_outcome"]
        order_price = round(price, 2)
        shares = self._calc_shares(bet_size, order_price)
        if shares <= 0:
            return {"success": False, "error": f"No se pudo calcular shares para price={order_price} bet={bet_size}"}

        from py_clob_client.clob_types import OrderArgs
        order_args = OrderArgs(price=order_price, size=shares, side="BUY", token_id=token_id)
        print(f"[AlertTrader] Order: price={order_price} shares={shares} usdc={round(shares*order_price,2)} type=GTC", flush=True)

        success, order_id, error_msg = await self._post_and_poll_order(order_args, token_id)

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
            "is_copy_trade": trade_info.get("is_copy_trade", False),
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

    async def _execute_dca(self, trade_info: dict, token_id: str, price: float, total_bet: float) -> dict:
        """v10: DCA — dividir la orden en múltiples partes con intervalos.
        Reduce price impact y verifica que el precio sigue favorable.
        """
        cfg = self._config
        splits = max(cfg.get("dca_splits", 2), 2)
        interval = cfg.get("dca_interval_sec", 30)
        split_size = total_bet / splits
        cid = trade_info["condition_id"]
        outcome = trade_info["buy_outcome"]

        total_shares = 0
        total_cost = 0
        order_ids = []
        fills = 0

        print(f"[AlertTrader] 🔄 DCA: {splits} órdenes de ${split_size:.2f} cada {interval}s", flush=True)

        for i in range(splits):
            if i > 0:
                await asyncio.sleep(interval)
                # Re-check precio actual antes de cada split
                _, current_price = await self._get_token_and_price(cid, outcome)
                if current_price and current_price > 0:
                    # Si el precio subió más de 5% desde el original, parar DCA
                    if current_price > price * 1.05:
                        print(f"[AlertTrader] ⚠️ DCA parado: precio subió a {current_price:.2f} (+{((current_price/price)-1)*100:.1f}%)",
                              flush=True)
                        break
                    price = current_price

            order_price = round(price, 2)
            shares = self._calc_shares(split_size, order_price)
            if shares <= 0:
                continue

            from py_clob_client.clob_types import OrderArgs
            order_args = OrderArgs(price=order_price, size=shares, side="BUY", token_id=token_id)
            print(f"[AlertTrader]   DCA [{i+1}/{splits}]: {shares} shares @ {order_price}", flush=True)

            success, order_id, error_msg = await self._post_and_poll_order(order_args, token_id)
            if success:
                total_shares += shares
                total_cost += shares * order_price
                order_ids.append(order_id)
                fills += 1

        if fills == 0:
            return {"success": False, "error": "DCA: ninguna orden llenada"}

        avg_price = total_cost / total_shares if total_shares > 0 else price
        trade_record = {
            "condition_id": cid,
            "order_id": ",".join(order_ids[:3]),
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
            "price": round(avg_price, 4),
            "size_usd": round(total_cost, 2),
            "shares": round(total_shares, 4),
            "token_id": token_id,
            "category": trade_info["category"],
            "wallet_hit_rate": trade_info["wallet_hit_rate"],
            "is_copy_trade": trade_info.get("is_copy_trade", False),
            "status": "filled",
            "error": None,
        }

        await self.db.record_alert_autotrade(trade_record)
        self._last_trade_time = time.time()
        self._trades_today.append(trade_record)
        self._open_positions[cid] = trade_record
        print(f"[AlertTrader] ✅ DCA COPY-TRADE: {outcome} en {trade_info['market_slug'][:40]} "
              f"${total_cost:.2f} @ avg {avg_price:.3f} ({fills}/{splits} fills, {total_shares:.2f} shares)",
              flush=True)
        return {"success": True, "order_id": order_ids[0] if order_ids else "", "trade": trade_record}

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

    async def process_alert(self, candidate, trade, is_copy_trade: bool = False):
        """Evaluar alerta y ejecutar trade si pasa filtros.
        Llamado desde main.py después de enviar alerta a Telegram.
        is_copy_trade=True bypasea filtros de score para wallets watchlisted.
        Si confirmación está activa, encola en vez de ejecutar.
        """
        if not self._client or not self._initialized:
            return

        # Copy Trade: ejecutar directo si está habilitado, sin filtro de score
        if is_copy_trade:
            await self._process_copy_trade(candidate, trade)
            return

        if not self._enabled:
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

    def _calc_copy_bet_size(self, wc: dict, insider_size: float) -> float:
        """Calcular bet size según config per-wallet.
        Modos: fixed, pct, range, proporcional.
        Retorna 0 si no debe copiar (presupuesto agotado, etc.)."""
        mode = wc.get("ct_mode", "fixed")
        budget = float(wc.get("ct_budget", 0))
        budget_used = float(wc.get("ct_budget_used", 0))
        remaining = budget - budget_used if budget > 0 else float('inf')

        if mode == "fixed":
            bet = float(wc.get("ct_fixed_amount", 5))
        elif mode == "pct":
            pct = float(wc.get("ct_pct", 5)) / 100.0
            bet = insider_size * pct
        elif mode == "range":
            pct = float(wc.get("ct_pct", 5)) / 100.0
            bet = insider_size * pct
            min_bet = float(wc.get("ct_min_bet", 2))
            max_bet = float(wc.get("ct_max_bet", 50))
            bet = max(min_bet, min(bet, max_bet))
        elif mode == "proporcional":
            # Mismo % que el insider usa de su capital total
            # Ej: insider tiene $100, apuesta $22 = 22%. Yo tengo $50 → 22% = $11
            insider_capital = float(wc.get("ct_insider_capital", 0))
            if insider_capital <= 0 or budget <= 0:
                print(f"[CopyTrade] ⚠️ Modo proporcional requiere capital insider y budget configurados", flush=True)
                return 0
            insider_pct = min(insider_size / insider_capital, 1.0)  # % que el insider usó (cap 100%)
            bet = insider_pct * budget  # Mismo % aplicado a mi budget
            # Aplicar min/max de seguridad
            min_bet = float(wc.get("ct_min_bet", 1))
            max_bet = float(wc.get("ct_max_bet", 50))
            if max_bet > 0:
                bet = min(bet, max_bet)
            if min_bet > 0:
                bet = max(bet, min_bet)
        else:
            bet = float(wc.get("ct_fixed_amount", 5))

        # No superar presupuesto restante
        if budget > 0 and bet > remaining:
            if remaining < 1:
                return 0  # Presupuesto agotado
            bet = remaining

        # Mínimo $1 para que sea viable en CLOB
        return max(bet, 1.0) if bet > 0 else 0

    async def _process_copy_trade(self, candidate, trade):
        """Ejecutar copy trade automático de wallet watchlisted.
        Lee config per-wallet: modo, bet size, presupuesto, filtros.
        Bypasea filtros de score."""
        cfg = self._config
        if not cfg.get("copy_trade_enabled"):
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._trades_today_date != today:
            await self._load_today_trades()

        # Filtro: max posiciones copy trade abiertas (global)
        ct_max_pos = cfg.get("copy_trade_max_positions", 3)
        ct_open = sum(1 for p in self._open_positions.values() if p.get("is_copy_trade"))
        if ct_open >= ct_max_pos:
            print(f"[CopyTrade] ⚠️ Max posiciones copy trade ({ct_max_pos}) alcanzado", flush=True)
            return

        # Filtro: max trades diarios copy trade (global)
        ct_max_daily = cfg.get("copy_trade_max_daily", 10)
        ct_today = sum(1 for t in self._trades_today if t.get("is_copy_trade"))
        if ct_today >= ct_max_daily:
            print(f"[CopyTrade] ⚠️ Max trades diarios copy trade ({ct_max_daily}) alcanzado", flush=True)
            return

        # Filtro: no duplicar posición en mismo mercado
        if trade.market_id in self._open_positions:
            return

        # Cooldown general entre trades
        now = time.time()
        if now - self._last_trade_time < 10:
            return

        # ── Config per-wallet ──
        wc = await self.db.get_wallet_copy_config(trade.wallet_address)
        if not wc:
            print(f"[CopyTrade] ⚠️ Sin config para {trade.wallet_address[:10]}, usando defaults", flush=True)
            wc = {"ct_enabled": True, "ct_mode": "fixed", "ct_fixed_amount": cfg.get("copy_trade_bet_size", 10),
                  "ct_budget": 0, "ct_budget_used": 0, "ct_pct": 5, "ct_min_bet": 2, "ct_max_bet": 50,
                  "ct_max_per_market": 0, "ct_min_trigger": 0, "ct_insider_capital": 0}

        # Filtro: wallet copy trade habilitado
        if not wc.get("ct_enabled", False):
            # Si no tiene ct_enabled pero está en watchlist, usar config global como fallback
            if wc.get("ct_budget", 0) == 0 and wc.get("ct_mode") == "fixed":
                wc["ct_enabled"] = True
                wc["ct_fixed_amount"] = cfg.get("copy_trade_bet_size", 10)
            else:
                return

        # Filtro per-wallet: min trigger (ignorar trades pequeños del insider)
        min_trigger = float(wc.get("ct_min_trigger", 0))
        if min_trigger > 0 and trade.size < min_trigger:
            print(f"[CopyTrade] ⏭️ Trade ${trade.size:.0f} < min_trigger ${min_trigger:.0f} de {trade.wallet_address[:10]}", flush=True)
            return

        # Filtro per-wallet: max por mercado (exposición a un mercado)
        max_per_mkt = float(wc.get("ct_max_per_market", 0))
        if max_per_mkt > 0:
            mkt_exposure = sum(
                t.get("size_usd", 0) for t in self._trades_today
                if t.get("is_copy_trade") and t.get("condition_id") == trade.market_id
                   and t.get("wallet_address", "").lower() == trade.wallet_address.lower()
            )
            if mkt_exposure >= max_per_mkt:
                print(f"[CopyTrade] ⚠️ Max por mercado ${max_per_mkt:.0f} alcanzado para {trade.market_slug[:30]}", flush=True)
                return

        # Filtro per-wallet: presupuesto agotado
        budget = float(wc.get("ct_budget", 0))
        budget_used = float(wc.get("ct_budget_used", 0))
        if budget > 0 and budget_used >= budget:
            print(f"[CopyTrade] ⚠️ Presupuesto agotado para {trade.wallet_address[:10]}: ${budget_used:.0f}/${budget:.0f}", flush=True)
            return

        # Calcular bet size según modo
        bet_size = self._calc_copy_bet_size(wc, trade.size)
        if bet_size <= 0:
            print(f"[CopyTrade] ⚠️ Bet size calculado = 0 para {trade.wallet_address[:10]}", flush=True)
            return

        trade_info = {
            "condition_id": trade.market_id,
            "market_slug": trade.market_slug,
            "market_question": trade.market_question,
            "wallet_address": trade.wallet_address,
            "insider_side": trade.side,
            "insider_outcome": trade.outcome,
            "buy_outcome": trade.outcome if trade.side == "BUY" else ("No" if trade.outcome == "Yes" else "Yes"),
            "insider_size": trade.size,
            "insider_price": trade.price,
            "alert_score": candidate.score,
            "triggers": f"⭐ COPY TRADE: {trade.wallet_address[:10]}... | modo={wc.get('ct_mode','fixed')}",
            "wallet_hit_rate": candidate.wallet_hit_rate or 0,
            "bet_size": bet_size,
            "category": trade.market_category or "",
            "is_copy_trade": True,
        }

        mode_label = wc.get("ct_mode", "fixed")
        budget_label = f" | budget=${budget_used:.0f}+{bet_size:.0f}/{budget:.0f}" if budget > 0 else ""
        print(f"[CopyTrade] 🚀 {trade.market_slug[:35]} | "
              f"wallet={trade.wallet_address[:10]} | {trade.side} {trade.outcome} | "
              f"modo={mode_label} bet=${bet_size:.2f}{budget_label}", flush=True)

        result = await self.execute_trade(trade_info)
        if result.get("success"):
            # Actualizar presupuesto usado
            if budget > 0:
                await self.db.update_wallet_budget_used(trade.wallet_address, bet_size)
            logger.info("copy_trade_executed",
                        market=trade.market_slug,
                        wallet=trade.wallet_address[:10],
                        side=trade.side,
                        outcome=trade.outcome,
                        size=bet_size,
                        mode=mode_label)
        else:
            print(f"[CopyTrade] ❌ Error: {result.get('error', 'unknown')}", flush=True)

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

    # ── Take Profit / Stop Loss / Trailing / Partial ────────────────────

    async def check_take_profits(self):
        """Revisar posiciones abiertas: TP fijo, partial TP, trailing stop, stop loss."""
        cfg = self._config
        if not self._open_positions or not self._client:
            return

        tp_enabled = cfg.get("take_profit_enabled", False)
        tp_pct = cfg.get("take_profit_pct", 0)
        sl_pct = cfg.get("stop_loss_pct", 0)
        trailing_enabled = cfg.get("trailing_stop_enabled", False)
        trailing_pct = cfg.get("trailing_stop_pct", 15)
        partial_enabled = cfg.get("partial_tp_enabled", False)
        partial_pct = cfg.get("partial_tp_pct", 30)
        partial_fraction = cfg.get("partial_tp_fraction", 50) / 100  # 0-1

        # Necesitamos al menos una feature activa
        if not tp_enabled and not trailing_enabled and not partial_enabled:
            return
        if tp_enabled and tp_pct <= 0 and sl_pct <= 0 and not trailing_enabled and not partial_enabled:
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

                        # ── Actualizar trailing high ──
                        if trailing_enabled:
                            prev_high = self._trailing_highs.get(cid, entry_price)
                            if current_price > prev_high:
                                self._trailing_highs[cid] = current_price

                        # ── Determinar acción ──
                        action = None
                        sell_fraction = 1.0  # 1.0 = vender todo

                        # 1. Partial Profit Taking: vender fracción al primer target
                        if partial_enabled and partial_pct > 0 and gain_pct >= partial_pct:
                            if cid not in self._partial_sold:
                                action = "partial_tp"
                                sell_fraction = partial_fraction
                                self._partial_sold.add(cid)

                        # 2. Take Profit completo (solo si no es partial o ya se hizo partial)
                        if not action and tp_enabled and tp_pct > 0 and gain_pct >= tp_pct:
                            if cid in self._partial_sold:
                                action = "take_profit"  # Vender el resto
                            elif not partial_enabled:
                                action = "take_profit"

                        # 3. Trailing Stop Loss: vender si cayó X% desde el máximo
                        if not action and trailing_enabled and trailing_pct > 0:
                            high = self._trailing_highs.get(cid, entry_price)
                            if high > entry_price:  # Solo si alguna vez estuvo en ganancia
                                drop_from_high = ((high - current_price) / high) * 100
                                if drop_from_high >= trailing_pct:
                                    action = "trailing_stop"

                        # 4. Stop Loss fijo
                        if not action and tp_enabled and sl_pct > 0 and gain_pct <= -sl_pct:
                            action = "stop_loss"

                        if not action:
                            continue

                        # ── Ejecutar venta ──
                        shares_to_sell = trade.get("shares", 0)
                        if sell_fraction < 1.0:
                            shares_to_sell = round(shares_to_sell * sell_fraction, 2)

                        sell_trade = dict(trade)
                        sell_trade["shares"] = shares_to_sell

                        sell_result = await self._sell_position(sell_trade, current_price)
                        if sell_result.get("success"):
                            pnl = shares_to_sell * current_price - shares_to_sell * entry_price

                            if sell_fraction < 1.0:
                                # Partial: actualizar shares restantes, no cerrar posición
                                remaining = trade.get("shares", 0) - shares_to_sell
                                self._open_positions[cid]["shares"] = round(remaining, 2)
                                self._open_positions[cid]["size_usd"] = round(remaining * entry_price, 2)
                                await self.db.update_alert_autotrade_shares(cid, round(remaining, 2))
                                print(f"[AlertTrader] 💰 PARTIAL TP ({partial_fraction*100:.0f}%): "
                                      f"{trade.get('market_slug', cid)[:40]} "
                                      f"vendido {shares_to_sell:.2f} shares @ {current_price:.2f} "
                                      f"({gain_pct:+.1f}%) PnL=${pnl:.2f} | queda {remaining:.2f}",
                                      flush=True)
                            else:
                                await self.db.resolve_alert_autotrade(cid, action, round(pnl, 2))
                                del self._open_positions[cid]
                                self._trailing_highs.pop(cid, None)
                                self._partial_sold.discard(cid)
                                emojis = {"take_profit": "💰", "stop_loss": "🛑", "trailing_stop": "📉"}
                                labels = {"take_profit": "TAKE PROFIT", "stop_loss": "STOP LOSS", "trailing_stop": "TRAILING STOP"}
                                print(f"[AlertTrader] {emojis.get(action, '💰')} {labels.get(action, action)}: "
                                      f"{trade.get('market_slug', cid)[:40]} "
                                      f"entrada={entry_price:.2f} → venta={current_price:.2f} "
                                      f"({gain_pct:+.1f}%) PnL=${pnl:.2f}",
                                      flush=True)

                            await self._load_today_trades()
                            self._check_drawdown()

                    except Exception as e:
                        print(f"[AlertTrader] Error check_take_profit {cid}: {e}", flush=True)

        except Exception as e:
            print(f"[AlertTrader] Error en check_take_profits: {e}", flush=True)

    def _check_drawdown(self):
        """Verificar max drawdown y pausar/reanudar trading."""
        cfg = self._config
        max_dd = cfg.get("max_drawdown", 0)
        if max_dd <= 0:
            return

        daily_pnl = sum(t.get("pnl", 0) for t in self._trades_today if t.get("resolved"))
        if self._peak_balance < daily_pnl:
            self._peak_balance = daily_pnl

        drawdown = self._peak_balance - daily_pnl
        if drawdown >= max_dd and not self._drawdown_paused:
            self._drawdown_paused = True
            print(f"[AlertTrader] 🚨 MAX DRAWDOWN alcanzado (${drawdown:.2f} >= ${max_dd:.2f}) — PAUSADO",
                  flush=True)
        elif drawdown < max_dd * 0.8 and self._drawdown_paused:
            self._drawdown_paused = False
            print(f"[AlertTrader] ✅ Drawdown recuperado — REANUDADO", flush=True)

    async def _sell_position(self, trade: dict, sell_price: float) -> dict:
        """Vender una posición abierta (SELL en CLOB con slippage para asegurar fill)."""
        try:
            token_id = trade.get("token_id", "")
            shares = trade.get("shares", 0)
            if not token_id or not shares:
                return {"success": False, "error": "Sin token_id o shares"}

            from py_clob_client.clob_types import OrderArgs, OrderType

            # Slippage negativo para asegurar que el SELL se ejecute
            actual_sell_price = max(round(sell_price - 0.02, 2), 0.01)

            order_args = OrderArgs(
                price=actual_sell_price,
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
                print(f"[AlertTrader] ⚠️ SELL FOK no ejecutado: {error_msg} — posición sigue abierta", flush=True)
                return {"success": False, "error": error_msg}

            order_id = resp_data.get("orderID", resp_data.get("order_id", "")) or ""
            return {"success": True, "order_id": order_id}

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
            "drawdown_paused": self._drawdown_paused,
            "trailing_positions": len(self._trailing_highs),
            "partial_sold": len(self._partial_sold),
        }
