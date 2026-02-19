"""Crypto Market Maker — Quoting bilateral (BUY Up + BUY Down) en mercados crypto up/down.

Estrategia:
1. Identifica mercados crypto up/down activos via CryptoArbDetector
2. Coloca órdenes BUY en AMBOS lados (Up y Down) simultáneamente
3. Captura spread cuando ambas órdenes se llenan (up_cost + down_cost < $1.00)
4. Sesgo direccional opcional basado en señales del detector
5. Re-quote dinámico cuando el precio se mueve
6. Paper mode para testear sin capital real

Flujo:
  tick() → _refresh_markets() → _quote_market() → _manage_open_orders() → _check_resolved()
"""
import asyncio
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Optional
import httpx
import structlog

from src import config

logger = structlog.get_logger()

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
MIN_CLOB_SHARES = Decimal('5')


@dataclass
class MMOrder:
    """Una orden del market maker (paper o real)."""
    id: str
    condition_id: str
    event_slug: str
    coin: str
    outcome: str           # "Up" o "Down"
    price: float
    shares: float
    cost_usdc: float
    token_id: str
    is_paper: bool
    status: str = "open"   # open, filled, cancelled, resolved
    result: str = ""       # win, loss, spread_captured
    pnl: float = 0.0
    rebate_est: float = 0.0
    created_ts: float = 0.0
    filled_ts: float = 0.0
    resolved_ts: float = 0.0
    resolution: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "condition_id": self.condition_id,
            "event_slug": self.event_slug,
            "coin": self.coin,
            "outcome": self.outcome,
            "price": self.price,
            "shares": round(self.shares, 2),
            "cost_usdc": round(self.cost_usdc, 2),
            "is_paper": self.is_paper,
            "status": self.status,
            "result": self.result,
            "pnl": round(self.pnl, 2),
            "rebate_est": round(self.rebate_est, 4),
            "created_ts": self.created_ts,
            "filled_ts": self.filled_ts,
            "resolved_ts": self.resolved_ts,
            "resolution": self.resolution,
            "age_sec": round(time.time() - self.created_ts, 0) if self.created_ts else 0,
        }


class CryptoMarketMaker:
    """Market maker bilateral para mercados crypto up/down de Polymarket."""

    def __init__(self, db, binance_feed=None, detector=None):
        self.db = db
        self._feed = binance_feed
        self._detector = detector
        self._client = None          # py-clob-client ClobClient
        self._enabled = False
        self._initialized = False
        self._config = {}
        # Órdenes activas: order_id -> MMOrder
        self._open_orders: dict[str, MMOrder] = {}
        # Inventario por event_slug: slug -> {up_shares, down_shares, up_cost, down_cost, condition_ids}
        self._inventory: dict[str, dict] = {}
        # Pares quotados: event_slug -> {up_order_id, down_order_id, last_quote_ts}
        self._quoted_markets: dict[str, dict] = {}
        # Cache de mercados elegibles
        self._markets_cache: list[dict] = []
        self._markets_cache_ts: float = 0
        # Historial resuelto
        self._resolved: list[MMOrder] = []
        # Stats
        self._stats = {
            "total_pnl": 0.0,
            "daily_pnl": 0.0,
            "daily_pnl_date": "",
            "fills": 0,
            "requotes": 0,
            "cancels": 0,
            "pairs_completed": 0,
            "total_rebates": 0.0,
        }
        self._order_counter = 0
        self._processing = False

    # ── Inicialización ─────────────────────────────────────────────

    async def initialize(self):
        """Cargar config desde DB e inicializar cliente CLOB."""
        try:
            raw = await self.db.get_config_bulk([
                "mm_enabled", "mm_bet_size", "mm_spread_target",
                "mm_max_inventory", "mm_max_markets", "mm_requote_sec",
                "mm_requote_threshold", "mm_fill_timeout_sec",
                "mm_bias_enabled", "mm_bias_strength",
                "mm_paper_mode", "mm_max_daily_loss",
                "mm_min_time_remaining_sec", "mm_rebate_rate",
            ], user_id=1)
            self._config = {
                "enabled": raw.get("mm_enabled") == "true",
                "bet_size": float(raw.get("mm_bet_size", 5)),
                "spread_target": float(raw.get("mm_spread_target", 0.04)),
                "max_inventory": float(raw.get("mm_max_inventory", 50)),
                "max_markets": int(raw.get("mm_max_markets", 3)),
                "requote_sec": int(raw.get("mm_requote_sec", 10)),
                "requote_threshold": float(raw.get("mm_requote_threshold", 0.02)),
                "fill_timeout_sec": int(raw.get("mm_fill_timeout_sec", 120)),
                "bias_enabled": raw.get("mm_bias_enabled") == "true",
                "bias_strength": float(raw.get("mm_bias_strength", 0.02)),
                "paper_mode": raw.get("mm_paper_mode", "true") != "false",
                "max_daily_loss": float(raw.get("mm_max_daily_loss", 20)),
                "min_time_remaining_sec": int(raw.get("mm_min_time_remaining_sec", 180)),
                "rebate_rate": float(raw.get("mm_rebate_rate", 0.005)),
            }
            self._enabled = self._config["enabled"]

            # Inicializar cliente CLOB si no es paper mode
            if self._enabled and not self._config["paper_mode"]:
                self._init_clob_client()

            self._initialized = True
            mode = "PAPER" if self._config["paper_mode"] else "REAL"
            status = "ACTIVADO" if self._enabled else "DESACTIVADO"
            print(f"[MarketMaker] {status} modo={mode} | bet=${self._config['bet_size']}/lado "
                  f"spread={self._config['spread_target']} max_mkts={self._config['max_markets']}"
                  f" bias={'ON' if self._config['bias_enabled'] else 'OFF'}", flush=True)
        except Exception as e:
            print(f"[MarketMaker] Error inicializando: {e}", flush=True)
            self._enabled = False

    def _init_clob_client(self):
        """Crear cliente CLOB reutilizando credenciales del AutoTrader."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            # Leer credenciales de env (mismas que AutoTrader)
            api_key = os.getenv("POLY_API_KEY", "")
            api_secret = os.getenv("POLY_API_SECRET", "")
            passphrase = os.getenv("POLY_PASSPHRASE", "")
            private_key = os.getenv("POLY_PRIVATE_KEY", "")
            funder = os.getenv("POLY_FUNDER_ADDRESS", "")

            if not api_key or not private_key:
                print("[MarketMaker] Sin credenciales CLOB — solo paper mode", flush=True)
                self._config["paper_mode"] = True
                return

            if private_key and not private_key.startswith("0x"):
                private_key = "0x" + private_key

            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=passphrase,
            )
            self._client = ClobClient(
                CLOB_HOST,
                key=private_key,
                chain_id=CHAIN_ID,
                signature_type=2,
                funder=funder or None,
                creds=creds,
            )
            print(f"[MarketMaker] Cliente CLOB inicializado OK", flush=True)
        except ImportError:
            print("[MarketMaker] py-clob-client no instalado — solo paper mode", flush=True)
            self._config["paper_mode"] = True
        except Exception as e:
            print(f"[MarketMaker] Error CLOB: {e} — solo paper mode", flush=True)
            self._config["paper_mode"] = True

    async def reload_config(self):
        """Recargar config desde DB."""
        await self.initialize()

    # ── Tick principal ─────────────────────────────────────────────

    async def tick(self):
        """Ciclo principal del market maker. Llamado cada 5s."""
        if not self._enabled or not self._initialized or self._processing:
            return
        self._processing = True
        try:
            # Resetear PnL diario si cambió el día
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if self._stats["daily_pnl_date"] != today:
                self._stats["daily_pnl"] = 0.0
                self._stats["daily_pnl_date"] = today

            # Check max daily loss
            if self._stats["daily_pnl"] <= -self._config["max_daily_loss"]:
                return

            # 1. Refrescar mercados elegibles (cada 60s)
            await self._refresh_markets()

            # 2. Quotear mercados (colocar pares bilaterales)
            await self._quote_markets()

            # 3. Gestionar órdenes abiertas (fills, timeouts)
            await self._manage_open_orders()

            # 4. Verificar mercados resueltos
            await self._check_resolved()

        except Exception as e:
            print(f"[MarketMaker] Error en tick: {e}", flush=True)
        finally:
            self._processing = False

    # ── Mercados elegibles ─────────────────────────────────────────

    async def _refresh_markets(self):
        """Obtener mercados crypto up/down elegibles del detector."""
        now = time.time()
        if now - self._markets_cache_ts < 60:
            return
        self._markets_cache_ts = now

        if not self._detector:
            self._markets_cache = []
            return

        eligible = []
        for cid, mdata in self._detector._active_markets.items():
            # Verificar tiempo restante
            end_date = mdata.get("end_date")
            if end_date:
                if end_date.tzinfo is None:
                    end_date = end_date.replace(tzinfo=timezone.utc)
                remaining = (end_date - datetime.now(timezone.utc)).total_seconds()
            else:
                remaining = 0

            if remaining < self._config["min_time_remaining_sec"]:
                continue

            # Necesitamos tokens con precios
            tokens = mdata.get("tokens", [])
            up_token = None
            down_token = None
            for tok in tokens:
                outcome = tok.get("outcome", "").lower()
                price = float(tok.get("price", 0))
                if outcome == "up" and 0.05 < price < 0.95:
                    up_token = tok
                elif outcome == "down" and 0.05 < price < 0.95:
                    down_token = tok

            if not up_token or not down_token:
                continue

            eligible.append({
                "condition_id": cid,
                "coin": mdata.get("coin", "?"),
                "event_slug": mdata.get("event_slug", ""),
                "question": mdata.get("question", ""),
                "end_date": end_date,
                "remaining_sec": remaining,
                "interval": mdata.get("interval", 900),
                "up_token_id": up_token.get("token_id", ""),
                "down_token_id": down_token.get("token_id", ""),
                "up_price": float(up_token.get("price", 0)),
                "down_price": float(down_token.get("price", 0)),
            })

        # Ordenar por tiempo restante (más tiempo = más estable para MM)
        eligible.sort(key=lambda x: x["remaining_sec"], reverse=True)
        self._markets_cache = eligible[:self._config["max_markets"] * 2]

        if eligible:
            print(f"[MarketMaker] {len(eligible)} mercados elegibles, top {len(self._markets_cache)}", flush=True)

    # ── Quoting bilateral ──────────────────────────────────────────

    async def _quote_markets(self):
        """Colocar pares de órdenes bilaterales en mercados elegibles."""
        now = time.time()
        cfg = self._config
        active_quotes = len(self._quoted_markets)

        for market in self._markets_cache:
            if active_quotes >= cfg["max_markets"]:
                break

            slug = market["event_slug"]

            # Ya tenemos quote activo para este mercado?
            if slug in self._quoted_markets:
                existing = self._quoted_markets[slug]
                # Re-quotear si pasó suficiente tiempo
                if now - existing.get("last_quote_ts", 0) < cfg["requote_sec"]:
                    continue
                # Verificar si necesita re-quote por cambio de precio
                needs_requote = await self._check_requote(slug, market)
                if not needs_requote:
                    continue
                # Cancelar órdenes anteriores antes de re-quotear
                await self._cancel_quote(slug)
                self._stats["requotes"] += 1

            # Verificar inventario máximo
            inv = self._inventory.get(slug, {})
            net_exposure = abs(inv.get("up_cost", 0) - inv.get("down_cost", 0))
            if net_exposure >= cfg["max_inventory"]:
                continue

            # Calcular sesgo direccional
            bias = self._get_directional_bias(market["condition_id"])

            # Calcular precios de quote
            up_price = market["up_price"]
            down_price = market["down_price"]
            spread = cfg["spread_target"]

            # Bid prices: comprar por debajo del ask actual
            # Sin sesgo: ambos lados iguales
            # Con sesgo up: bid más alto en Up (más agresivo), más bajo en Down
            bid_up = round(max(up_price - spread / 2 + bias, 0.01), 2)
            bid_down = round(max(down_price - spread / 2 - bias, 0.01), 2)

            # SEGURIDAD: bid_up + bid_down DEBE ser < $1.00 para que el spread sea positivo
            if bid_up + bid_down >= 0.99:
                # Reducir ambos proporcionalmente
                total = bid_up + bid_down
                bid_up = round(bid_up * 0.98 / total, 2)
                bid_down = round(bid_down * 0.98 / total, 2)

            # No quotear si precios inválidos
            if bid_up <= 0.01 or bid_down <= 0.01 or bid_up >= 0.99 or bid_down >= 0.99:
                continue

            # Colocar par bilateral
            await self._place_bilateral_quote(market, bid_up, bid_down, cfg["bet_size"])
            active_quotes += 1

    async def _place_bilateral_quote(self, market: dict, bid_up: float, bid_down: float, bet_size: float):
        """Colocar un par de órdenes BUY Up + BUY Down."""
        slug = market["event_slug"]
        cid = market["condition_id"]
        coin = market["coin"]
        is_paper = self._config["paper_mode"]
        now = time.time()

        # Calcular shares para cada lado
        up_shares, up_cost = self._calc_shares(bet_size, bid_up)
        down_shares, down_cost = self._calc_shares(bet_size, bid_down)

        if up_shares <= 0 or down_shares <= 0:
            return

        # Crear órdenes
        self._order_counter += 1
        up_order_id = f"mm-up-{self._order_counter}-{int(now)}"
        self._order_counter += 1
        down_order_id = f"mm-dn-{self._order_counter}-{int(now)}"

        up_order = MMOrder(
            id=up_order_id, condition_id=cid, event_slug=slug,
            coin=coin, outcome="Up", price=bid_up, shares=up_shares,
            cost_usdc=up_cost, token_id=market["up_token_id"],
            is_paper=is_paper, created_ts=now,
        )
        down_order = MMOrder(
            id=down_order_id, condition_id=cid, event_slug=slug,
            coin=coin, outcome="Down", price=bid_down, shares=down_shares,
            cost_usdc=down_cost, token_id=market["down_token_id"],
            is_paper=is_paper, created_ts=now,
        )

        if not is_paper and self._client:
            # Modo REAL: colocar en CLOB
            real_up_id = await self._post_clob_order(market["up_token_id"], bid_up, up_shares)
            real_down_id = await self._post_clob_order(market["down_token_id"], bid_down, down_shares)
            if real_up_id:
                up_order.id = real_up_id
            if real_down_id:
                down_order.id = real_down_id

        self._open_orders[up_order.id] = up_order
        self._open_orders[down_order.id] = down_order
        self._quoted_markets[slug] = {
            "up_order_id": up_order.id,
            "down_order_id": down_order.id,
            "last_quote_ts": now,
            "condition_id": cid,
        }

        spread_captured = round(1.0 - bid_up - bid_down, 4)
        mode_tag = "📝" if is_paper else "💰"
        print(f"[MarketMaker] {mode_tag} QUOTE {coin}: "
              f"Up@${bid_up:.2f}({up_shares:.1f}sh) + Down@${bid_down:.2f}({down_shares:.1f}sh) "
              f"= ${up_cost + down_cost:.2f} spread_est=${spread_captured:.4f}", flush=True)

    async def _post_clob_order(self, token_id: str, price: float, shares: float) -> Optional[str]:
        """Colocar orden real en CLOB. Retorna order_id o None."""
        if not self._client:
            return None
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            loop = asyncio.get_running_loop()
            order_args = OrderArgs(
                price=price,
                size=shares,
                side="BUY",
                token_id=token_id,
            )
            signed = await loop.run_in_executor(None, self._client.create_order, order_args)
            resp = await loop.run_in_executor(None, self._client.post_order, signed, OrderType.GTC)
            if isinstance(resp, dict):
                return resp.get("orderID", "") or None
            return getattr(resp, "orderID", None)
        except Exception as e:
            print(f"[MarketMaker] Error CLOB order: {e}", flush=True)
            return None

    def _calc_shares(self, bet_size: float, price: float) -> tuple[float, float]:
        """Calcular shares y costo real. Retorna (shares, actual_usdc)."""
        if price <= 0 or price >= 1:
            return 0.0, 0.0
        d_price = Decimal(str(price))
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
        actual_usdc = float((d_shares * d_price).quantize(Decimal('0.01')))
        return float(d_shares), actual_usdc

    def _get_directional_bias(self, condition_id: str) -> float:
        """Sesgo direccional basado en señales del detector. >0 = favorecer Up."""
        if not self._config.get("bias_enabled") or not self._detector:
            return 0.0
        try:
            signals = self._detector.get_recent_signals(50)
            for s in signals:
                if s.get("condition_id") == condition_id or \
                   s.get("event_slug") and s["event_slug"] in self._quoted_markets:
                    confidence = s.get("confidence", 0)
                    if confidence >= 60:
                        bias = self._config.get("bias_strength", 0.02)
                        return bias if s.get("direction") == "up" else -bias
        except Exception:
            pass
        return 0.0

    async def _check_requote(self, slug: str, market: dict) -> bool:
        """Verificar si necesitamos re-quotear por cambio de precio."""
        existing = self._quoted_markets.get(slug, {})
        up_oid = existing.get("up_order_id")
        down_oid = existing.get("down_order_id")

        if up_oid and up_oid in self._open_orders:
            old_up = self._open_orders[up_oid]
            if old_up.status == "open":
                diff = abs(market["up_price"] - old_up.price - self._config["spread_target"] / 2)
                if diff >= self._config["requote_threshold"]:
                    return True

        if down_oid and down_oid in self._open_orders:
            old_dn = self._open_orders[down_oid]
            if old_dn.status == "open":
                diff = abs(market["down_price"] - old_dn.price - self._config["spread_target"] / 2)
                if diff >= self._config["requote_threshold"]:
                    return True

        return False

    async def _cancel_quote(self, slug: str):
        """Cancelar par de órdenes de un mercado."""
        existing = self._quoted_markets.pop(slug, {})
        for key in ("up_order_id", "down_order_id"):
            oid = existing.get(key)
            if oid and oid in self._open_orders:
                order = self._open_orders[oid]
                if order.status == "open":
                    order.status = "cancelled"
                    self._stats["cancels"] += 1
                    # Cancelar en CLOB si es real
                    if not order.is_paper and self._client:
                        try:
                            loop = asyncio.get_running_loop()
                            await loop.run_in_executor(None, self._client.cancel, oid)
                        except Exception:
                            pass
                    del self._open_orders[oid]

    # ── Gestión de órdenes abiertas ────────────────────────────────

    async def _manage_open_orders(self):
        """Verificar fills y timeouts de órdenes abiertas."""
        now = time.time()
        to_remove = []

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Agrupar por condition_id para hacer 1 request por mercado
                cid_orders: dict[str, list[str]] = {}
                for oid, order in self._open_orders.items():
                    if order.status != "open":
                        continue
                    cid_orders.setdefault(order.condition_id, []).append(oid)

                for cid, order_ids in cid_orders.items():
                    try:
                        resp = await client.get(f"{CLOB_HOST}/markets/{cid}")
                        if resp.status_code != 200:
                            continue
                        data = resp.json()

                        # Si el mercado cerró, mover a resolución
                        if data.get("closed"):
                            for oid in order_ids:
                                if oid in self._open_orders:
                                    self._open_orders[oid].status = "filled"  # Tratar como fill para resolver
                            continue

                        tokens = data.get("tokens", [])
                        price_map = {}
                        for tok in tokens:
                            outcome = tok.get("outcome", "").lower()
                            price_map[outcome] = float(tok.get("price", 0))

                        for oid in order_ids:
                            order = self._open_orders.get(oid)
                            if not order or order.status != "open":
                                continue

                            current_price = price_map.get(order.outcome.lower(), 0)
                            if current_price <= 0:
                                continue

                            if order.is_paper:
                                # Paper: simular fills realistas para maker orders
                                age = now - order.created_ts
                                price_diff = current_price - order.price  # >0 = mercado encima de nuestro bid

                                should_fill = False
                                if current_price <= order.price:
                                    # Caso 1: precio de mercado tocó nuestro bid
                                    should_fill = True
                                elif price_diff < 0.08:
                                    # Caso 2: simular fill natural — en mercados reales,
                                    # maker orders 2-4 cents debajo del mid se llenan en 10-60s
                                    # Probabilidad crece con tiempo, decrece con distancia al precio
                                    fill_prob = min(0.4, (age / 90.0) * max(0.05, 1.0 - price_diff * 12))
                                    should_fill = random.random() < fill_prob

                                if should_fill:
                                    order.status = "filled"
                                    order.filled_ts = now
                                    self._stats["fills"] += 1
                                    self._update_inventory(order)
                                    rebate = round(order.cost_usdc * self._config["rebate_rate"], 4)
                                    order.rebate_est = rebate
                                    self._stats["total_rebates"] += rebate
                                    print(f"[MarketMaker] ✅ FILL: {order.coin} {order.outcome} "
                                          f"@${order.price:.2f} (mercado=${current_price:.2f}) "
                                          f"age={age:.0f}s", flush=True)
                                elif age > self._config["fill_timeout_sec"]:
                                    order.status = "cancelled"
                                    to_remove.append(oid)
                                    self._stats["cancels"] += 1
                            else:
                                # Real: poll status del CLOB
                                try:
                                    loop = asyncio.get_running_loop()
                                    info = await loop.run_in_executor(
                                        None, self._client.get_order, oid
                                    )
                                    status = info.get("status", "").lower() if isinstance(info, dict) else ""
                                    if status == "matched":
                                        order.status = "filled"
                                        order.filled_ts = now
                                        self._stats["fills"] += 1
                                        self._update_inventory(order)
                                        rebate = round(order.cost_usdc * self._config["rebate_rate"], 4)
                                        order.rebate_est = rebate
                                        self._stats["total_rebates"] += rebate
                                        print(f"[MarketMaker] ✅ FILL REAL: {order.coin} {order.outcome} "
                                              f"@${order.price:.2f}", flush=True)
                                    elif status in ("cancelled", "expired", ""):
                                        order.status = "cancelled"
                                        to_remove.append(oid)
                                    elif now - order.created_ts > self._config["fill_timeout_sec"]:
                                        # Cancelar por timeout
                                        try:
                                            await loop.run_in_executor(None, self._client.cancel, oid)
                                        except Exception:
                                            pass
                                        order.status = "cancelled"
                                        to_remove.append(oid)
                                        self._stats["cancels"] += 1
                                except Exception as e:
                                    print(f"[MarketMaker] Error polling orden {oid[:16]}: {e}", flush=True)

                    except Exception as e:
                        print(f"[MarketMaker] Error gestionando {cid[:12]}: {e}", flush=True)

        except Exception as e:
            print(f"[MarketMaker] Error en manage_orders: {e}", flush=True)

        # Limpiar órdenes canceladas
        for oid in to_remove:
            order = self._open_orders.pop(oid, None)
            if order:
                # Limpiar de quoted_markets si aplica
                slug = order.event_slug
                if slug in self._quoted_markets:
                    qm = self._quoted_markets[slug]
                    if qm.get("up_order_id") == oid or qm.get("down_order_id") == oid:
                        self._quoted_markets.pop(slug, None)

    def _update_inventory(self, order: MMOrder):
        """Actualizar inventario cuando una orden se llena."""
        slug = order.event_slug
        if slug not in self._inventory:
            self._inventory[slug] = {
                "up_shares": 0.0, "down_shares": 0.0,
                "up_cost": 0.0, "down_cost": 0.0,
                "condition_id": order.condition_id,
                "coin": order.coin,
            }
        inv = self._inventory[slug]
        if order.outcome.lower() == "up":
            inv["up_shares"] += order.shares
            inv["up_cost"] += order.cost_usdc
        else:
            inv["down_shares"] += order.shares
            inv["down_cost"] += order.cost_usdc

    # ── Resolución de mercados ─────────────────────────────────────

    async def _check_resolved(self):
        """Verificar mercados cerrados y calcular PnL."""
        to_remove_inv = []

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for slug, inv in list(self._inventory.items()):
                    cid = inv.get("condition_id", "")
                    if not cid:
                        continue

                    try:
                        resp = await client.get(f"{CLOB_HOST}/markets/{cid}")
                        if resp.status_code != 200:
                            continue
                        data = resp.json()

                        if not data.get("closed"):
                            continue

                        # Determinar ganador
                        winner = self._get_winner(data)
                        if not winner:
                            continue

                        # Calcular PnL
                        total_cost = inv["up_cost"] + inv["down_cost"]
                        if winner == "up":
                            payout = inv["up_shares"] * 1.0  # Cada share Up paga $1
                        else:
                            payout = inv["down_shares"] * 1.0  # Cada share Down paga $1

                        pnl = round(payout - total_cost, 2)
                        rebates = 0.0

                        # Calcular rebates de órdenes llenadas
                        for oid, order in list(self._open_orders.items()):
                            if order.event_slug == slug and order.status == "filled":
                                rebates += order.rebate_est
                                order.status = "resolved"
                                order.resolved_ts = time.time()
                                order.resolution = winner.capitalize()
                                won = order.outcome.lower() == winner
                                order.result = "win" if won else "loss"
                                order.pnl = round(order.shares * 1.0 - order.cost_usdc, 2) if won else round(-order.cost_usdc, 2)
                                self._resolved.append(order)

                        total_pnl = round(pnl + rebates, 2)
                        self._stats["total_pnl"] += total_pnl
                        self._stats["daily_pnl"] += total_pnl
                        if inv["up_shares"] > 0 and inv["down_shares"] > 0:
                            self._stats["pairs_completed"] += 1

                        emoji = "✅" if total_pnl >= 0 else "❌"
                        print(f"[MarketMaker] {emoji} RESOLVED {inv['coin']}: winner={winner.upper()} "
                              f"payout=${payout:.2f} cost=${total_cost:.2f} PnL=${total_pnl:+.2f} "
                              f"(rebates=${rebates:.4f})", flush=True)

                        # Registrar en DB
                        try:
                            await self.db.record_mm_trade({
                                "condition_id": cid,
                                "event_slug": slug,
                                "coin": inv["coin"],
                                "up_shares": inv["up_shares"],
                                "down_shares": inv["down_shares"],
                                "up_cost": inv["up_cost"],
                                "down_cost": inv["down_cost"],
                                "total_cost": total_cost,
                                "payout": payout,
                                "pnl": total_pnl,
                                "rebates": rebates,
                                "winner": winner,
                                "is_paper": self._config["paper_mode"],
                            })
                        except Exception as e:
                            print(f"[MarketMaker] Error guardando trade en DB: {e}", flush=True)

                        to_remove_inv.append(slug)

                    except Exception as e:
                        print(f"[MarketMaker] Error resolviendo {slug}: {e}", flush=True)

        except Exception as e:
            print(f"[MarketMaker] Error en check_resolved: {e}", flush=True)

        # Limpiar inventario resuelto
        for slug in to_remove_inv:
            self._inventory.pop(slug, None)
            self._quoted_markets.pop(slug, None)

        # Limpiar órdenes resueltas del dict activo
        resolved_ids = [oid for oid, o in self._open_orders.items() if o.status == "resolved"]
        for oid in resolved_ids:
            self._open_orders.pop(oid, None)

        # Mantener máx 500 resueltas en historial
        if len(self._resolved) > 500:
            self._resolved = self._resolved[-500:]

    def _get_winner(self, market_data: dict) -> Optional[str]:
        """Determinar ganador de un mercado cerrado."""
        tokens = market_data.get("tokens", [])
        for tok in tokens:
            if tok.get("winner") is True:
                return tok.get("outcome", "").lower()
            if float(tok.get("price", 0)) >= 0.95:
                return tok.get("outcome", "").lower()
        # Fallback: outcomePrices
        import json
        op = market_data.get("outcomePrices", "")
        outcomes_raw = market_data.get("outcomes", "")
        try:
            prices = json.loads(op) if isinstance(op, str) and op else []
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) and outcomes_raw else []
            for i, p in enumerate(prices):
                if float(p) >= 0.90 and i < len(outcomes):
                    return outcomes[i].lower()
        except Exception:
            pass
        return None

    # ── Estado y consultas ─────────────────────────────────────────

    def get_status(self) -> dict:
        """Estado completo para dashboard."""
        open_orders = [o for o in self._open_orders.values() if o.status == "open"]
        filled_orders = [o for o in self._open_orders.values() if o.status == "filled"]
        total_resolved = len(self._resolved)
        wins = sum(1 for o in self._resolved if o.result == "win")

        return {
            "enabled": self._enabled,
            "paper_mode": self._config.get("paper_mode", True),
            "config": self._config,
            "active_markets": len(self._quoted_markets),
            "open_orders": len(open_orders),
            "filled_pending": len(filled_orders),
            "inventory": {slug: {
                "coin": inv["coin"],
                "up_shares": round(inv["up_shares"], 2),
                "down_shares": round(inv["down_shares"], 2),
                "up_cost": round(inv["up_cost"], 2),
                "down_cost": round(inv["down_cost"], 2),
                "total_cost": round(inv["up_cost"] + inv["down_cost"], 2),
                "net_exposure": round(abs(inv["up_cost"] - inv["down_cost"]), 2),
            } for slug, inv in self._inventory.items()},
            "stats": {
                **self._stats,
                "total_pnl": round(self._stats["total_pnl"], 2),
                "daily_pnl": round(self._stats["daily_pnl"], 2),
                "total_rebates": round(self._stats["total_rebates"], 4),
                "total_resolved": total_resolved,
                "wins": wins,
                "losses": total_resolved - wins,
                "win_rate": round(wins / total_resolved * 100, 1) if total_resolved > 0 else 0,
            },
        }

    def get_open_orders(self) -> list[dict]:
        """Órdenes abiertas para dashboard."""
        return [o.to_dict() for o in self._open_orders.values()
                if o.status in ("open", "filled")]

    def get_resolved(self, limit: int = 100) -> list[dict]:
        """Historial resuelto para dashboard."""
        return [o.to_dict() for o in reversed(self._resolved[-limit:])]

    def get_inventory(self) -> list[dict]:
        """Inventario por mercado."""
        result = []
        for slug, inv in self._inventory.items():
            result.append({
                "event_slug": slug,
                "coin": inv["coin"],
                "condition_id": inv["condition_id"],
                "up_shares": round(inv["up_shares"], 2),
                "down_shares": round(inv["down_shares"], 2),
                "up_cost": round(inv["up_cost"], 2),
                "down_cost": round(inv["down_cost"], 2),
                "total_cost": round(inv["up_cost"] + inv["down_cost"], 2),
                "est_pnl_if_up": round(inv["up_shares"] * 1.0 - inv["up_cost"] - inv["down_cost"], 2),
                "est_pnl_if_down": round(inv["down_shares"] * 1.0 - inv["up_cost"] - inv["down_cost"], 2),
            })
        return result

    def reset(self):
        """Resetear todo el estado."""
        # Cancelar órdenes reales si existen
        self._open_orders.clear()
        self._inventory.clear()
        self._quoted_markets.clear()
        self._resolved.clear()
        self._markets_cache.clear()
        self._stats = {
            "total_pnl": 0.0, "daily_pnl": 0.0, "daily_pnl_date": "",
            "fills": 0, "requotes": 0, "cancels": 0,
            "pairs_completed": 0, "total_rebates": 0.0,
        }
        self._order_counter = 0
        print("[MarketMaker] Estado reseteado completamente", flush=True)


# Mantener compatibilidad con import existente en main.py
MarketMakerBot = CryptoMarketMaker
