"""Paper Trading para estrategia Maker Orders en crypto 15-min.

Simula órdenes maker (post-only GTC) en tiempo real sin usar dinero real.
Verifica si las órdenes se habrían llenado basándose en precios reales
de Polymarket, y calcula PnL como maker (sin fee + rebate estimado).

Flujo:
1. Recibe señales del CryptoArbDetector (igual que AutoTrader)
2. Calcula precio maker (ask - spread_offset)
3. Registra "paper order" simulada
4. Monitorea precios reales para simular fill
5. Cuando el mercado cierra, resuelve PnL
6. Muestra comparativa Taker vs Maker
"""
import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import structlog
import httpx

from src import config

logger = structlog.get_logger()

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class PaperOrder:
    """Una orden maker simulada."""
    id: str                       # ID único
    condition_id: str
    coin: str
    direction: str                # "up" o "down"
    side: str                     # "BUY"
    price: float                  # Precio maker (ask - offset)
    shares: float                 # Shares calculadas
    bet_size: float               # USDC invertido
    spread_offset: float          # Offset usado
    mode: str                     # "maker" o "hybrid"
    strategy: str                 # "score" o "early_entry"
    event_slug: str
    market_question: str
    status: str = "open"          # "open", "filled", "expired", "resolved"
    result: str = ""              # "win", "loss", "" (vacío si no resuelto)
    pnl_maker: float = 0.0       # PnL como maker
    pnl_taker: float = 0.0       # PnL hipotético como taker (para comparativa)
    rebate_est: float = 0.0      # Rebate estimado
    fill_price: float = 0.0      # Precio al que se "llenó"
    created_ts: float = 0.0
    filled_ts: float = 0.0
    resolved_ts: float = 0.0
    resolution: str = ""          # "Up" o "Down" (resultado real del mercado)
    score: float = 0.0            # Score de la señal original
    edge_pct: float = 0.0
    confidence: float = 0.0
    poly_odds_at_signal: float = 0.0  # Odds de Polymarket cuando llegó la señal

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "condition_id": self.condition_id,
            "coin": self.coin,
            "direction": self.direction,
            "price": self.price,
            "shares": round(self.shares, 2),
            "bet_size": self.bet_size,
            "spread_offset": self.spread_offset,
            "mode": self.mode,
            "strategy": self.strategy,
            "event_slug": self.event_slug,
            "market_question": self.market_question[:80],
            "status": self.status,
            "result": self.result,
            "pnl_maker": round(self.pnl_maker, 2),
            "pnl_taker": round(self.pnl_taker, 2),
            "rebate_est": round(self.rebate_est, 4),
            "fill_price": self.fill_price,
            "created_ts": self.created_ts,
            "filled_ts": self.filled_ts,
            "resolved_ts": self.resolved_ts,
            "resolution": self.resolution,
            "score": round(self.score, 2),
            "edge_pct": round(self.edge_pct, 1),
            "confidence": round(self.confidence, 1),
            "poly_odds_at_signal": self.poly_odds_at_signal,
            "age_sec": round(time.time() - self.created_ts, 0) if self.created_ts else 0,
        }


class MakerPaperTrader:
    """Motor de paper trading para órdenes maker en crypto 15-min."""

    def __init__(self):
        self._enabled = False
        self._orders: dict[str, PaperOrder] = {}  # id -> PaperOrder
        self._resolved: list[PaperOrder] = []  # Órdenes resueltas (max 500)
        self._config = {
            "bet_size": config.PAPER_BET_SIZE,
            "spread_offset": config.PAPER_SPREAD_OFFSET,
            "initial_capital": config.PAPER_INITIAL_CAPITAL,
            "mode": config.PAPER_MODE,
            "fill_timeout_sec": config.PAPER_FILL_TIMEOUT_SEC,
            "rebate_rate": config.PAPER_REBATE_RATE,
            "taker_fee_rate": config.PAPER_TAKER_FEE_RATE,
        }
        self._capital = config.PAPER_INITIAL_CAPITAL
        self._total_pnl_maker = 0.0
        self._total_pnl_taker = 0.0
        self._total_rebates = 0.0
        self._fills = 0
        self._no_fills = 0
        self._wins = 0
        self._losses = 0
        self._order_counter = 0
        self._processing = False

    def configure(self, cfg: dict):
        """Actualizar configuración desde dashboard."""
        if "enabled" in cfg:
            self._enabled = cfg["enabled"]
        if "bet_size" in cfg:
            self._config["bet_size"] = float(cfg["bet_size"])
        if "spread_offset" in cfg:
            self._config["spread_offset"] = float(cfg["spread_offset"])
        if "initial_capital" in cfg:
            self._config["initial_capital"] = float(cfg["initial_capital"])
            self._capital = float(cfg["initial_capital"])
        if "mode" in cfg:
            self._config["mode"] = cfg["mode"]
        if "fill_timeout_sec" in cfg:
            self._config["fill_timeout_sec"] = int(cfg["fill_timeout_sec"])
        status = "ACTIVADO" if self._enabled else "DESACTIVADO"
        print(f"[PaperTrader] {status} | bet=${self._config['bet_size']} "
              f"spread=${self._config['spread_offset']} mode={self._config['mode']}",
              flush=True)

    # ── Recibir señales ────────────────────────────────────────────

    async def process_signals(self, signals: list[dict]):
        """Evaluar señales y crear paper orders. Llamado desde main loop."""
        if not self._enabled or self._processing:
            return
        self._processing = True
        try:
            for signal in signals:
                await self._create_paper_order(signal)
        finally:
            self._processing = False

    async def _create_paper_order(self, signal: dict):
        """Crear una paper order maker a partir de una señal."""
        cid = signal.get("condition_id", "")
        if not cid:
            return

        # No duplicar: si ya hay una orden abierta para este mercado
        for order in self._orders.values():
            if order.condition_id == cid and order.status in ("open", "filled"):
                return
            # No duplicar por event_slug tampoco
            slug = signal.get("event_slug", "")
            if slug and order.event_slug == slug and order.status in ("open", "filled"):
                return

        # Calcular precio maker
        poly_odds = signal.get("poly_odds", 0.50)
        spread_offset = self._config["spread_offset"]
        maker_price = round(max(poly_odds - spread_offset, 0.01), 2)

        # Calcular shares
        bet_size = self._config["bet_size"]
        if maker_price <= 0:
            return
        shares = round(bet_size / maker_price, 2)
        if shares < 1:
            return

        self._order_counter += 1
        order_id = f"paper-{self._order_counter}-{int(time.time())}"

        score_details = signal.get("score_details", {})
        score = score_details.get("score", 0) if isinstance(score_details, dict) else 0

        order = PaperOrder(
            id=order_id,
            condition_id=cid,
            coin=signal.get("coin", "?"),
            direction=signal.get("direction", "?"),
            side="BUY",
            price=maker_price,
            shares=shares,
            bet_size=bet_size,
            spread_offset=spread_offset,
            mode=self._config["mode"],
            strategy=signal.get("strategy", "score"),
            event_slug=signal.get("event_slug", ""),
            market_question=signal.get("market_question", ""),
            status="open",
            created_ts=time.time(),
            score=score,
            edge_pct=signal.get("edge_pct", 0),
            confidence=signal.get("confidence", 0),
            poly_odds_at_signal=poly_odds,
        )

        self._orders[order_id] = order
        print(f"[PaperTrader] 📝 PAPER ORDER: {order.coin} {order.direction.upper()} "
              f"@ ${maker_price:.2f} (ask=${poly_odds:.2f} - offset=${spread_offset}) "
              f"shares={shares} ${bet_size}", flush=True)

    # ── Monitoreo de órdenes ───────────────────────────────────────

    async def manage_orders(self):
        """Verificar fills y resolver órdenes. Llamado cada 10s desde main loop."""
        if not self._enabled or not self._orders:
            return

        now = time.time()
        to_remove = []

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for order_id, order in list(self._orders.items()):
                    if order.status == "resolved":
                        to_remove.append(order_id)
                        continue

                    try:
                        # Obtener precio actual del mercado
                        resp = await client.get(f"{CLOB_HOST}/markets/{order.condition_id}")
                        if resp.status_code != 200:
                            continue
                        data = resp.json()

                        # Si el mercado cerró, resolver
                        if data.get("closed"):
                            await self._resolve_order(order, data)
                            to_remove.append(order_id)
                            continue

                        tokens = data.get("tokens", [])
                        current_price = None
                        for tok in tokens:
                            outcome = tok.get("outcome", "").lower()
                            if (order.direction == "up" and outcome == "up") or \
                               (order.direction == "down" and outcome == "down"):
                                current_price = float(tok.get("price", 0))
                                break

                        if current_price is None:
                            continue

                        # ── Simular fill de orden maker ──
                        if order.status == "open":
                            # Una orden maker BUY se llena cuando el ask baja
                            # a nuestro precio o menos
                            if current_price <= order.price:
                                order.status = "filled"
                                order.filled_ts = now
                                order.fill_price = order.price
                                self._fills += 1
                                print(f"[PaperTrader] ✅ FILL: {order.coin} "
                                      f"{order.direction.upper()} @ ${order.price:.2f} "
                                      f"(mercado bajó a ${current_price:.2f})", flush=True)

                            # Timeout: si no se llenó en X segundos, expirar
                            elif now - order.created_ts > self._config["fill_timeout_sec"]:
                                order.status = "expired"
                                self._no_fills += 1
                                to_remove.append(order_id)
                                self._add_resolved(order)
                                print(f"[PaperTrader] ⏰ EXPIRED: {order.coin} "
                                      f"{order.direction.upper()} @ ${order.price:.2f} "
                                      f"(no fill en {self._config['fill_timeout_sec']}s)",
                                      flush=True)

                    except Exception as e:
                        print(f"[PaperTrader] Error gestionando {order_id}: {e}", flush=True)

        except Exception as e:
            print(f"[PaperTrader] Error en manage_orders: {e}", flush=True)

        # Limpiar órdenes resueltas del dict activo
        for oid in to_remove:
            self._orders.pop(oid, None)

    async def _resolve_order(self, order: PaperOrder, market_data: dict):
        """Resolver una orden cuando el mercado cierra."""
        tokens = market_data.get("tokens", [])
        winner = None
        for tok in tokens:
            if tok.get("winner") is True:
                winner = tok.get("outcome", "").lower()
                break
            # Fallback: precio >= 0.95
            if float(tok.get("price", 0)) >= 0.95:
                winner = tok.get("outcome", "").lower()
                break

        if not winner:
            # Intentar desde outcomePrices
            import json
            op = market_data.get("outcomePrices", "")
            outcomes_raw = market_data.get("outcomes", "")
            try:
                prices = json.loads(op) if isinstance(op, str) and op else []
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) and outcomes_raw else []
                for i, p in enumerate(prices):
                    if float(p) >= 0.90 and i < len(outcomes):
                        winner = outcomes[i].lower()
                        break
            except Exception:
                pass

        if not winner:
            # No se pudo resolver — marcar como expired
            order.status = "expired"
            self._add_resolved(order)
            return

        order.resolution = winner.capitalize()
        order.resolved_ts = time.time()
        order.status = "resolved"

        # Solo calcular PnL si la orden fue llenada
        if order.fill_price > 0:
            won = (order.direction == "up" and winner == "up") or \
                  (order.direction == "down" and winner == "down")

            if won:
                order.result = "win"
                self._wins += 1
                # Maker: shares pagan $1 cada una, sin fee + rebate
                order.pnl_maker = round(order.shares - order.bet_size, 2)
                order.rebate_est = round(order.bet_size * self._config["rebate_rate"], 4)
                order.pnl_maker += order.rebate_est
                # Taker comparativa: mismo trade pero con fee
                taker_shares = order.bet_size / order.poly_odds_at_signal
                taker_fee = order.bet_size * self._config["taker_fee_rate"]
                order.pnl_taker = round(taker_shares - order.bet_size - taker_fee, 2)
            else:
                order.result = "loss"
                self._losses += 1
                # Maker: pierde el bet_size, pero sin fee extra
                order.pnl_maker = round(-order.bet_size, 2)
                order.rebate_est = round(order.bet_size * self._config["rebate_rate"], 4)
                order.pnl_maker += order.rebate_est
                # Taker comparativa: pierde bet_size + fee
                taker_fee = order.bet_size * self._config["taker_fee_rate"]
                order.pnl_taker = round(-order.bet_size - taker_fee, 2)

            self._total_pnl_maker += order.pnl_maker
            self._total_pnl_taker += order.pnl_taker
            self._total_rebates += order.rebate_est
            self._capital += order.pnl_maker

            emoji = "✅" if order.result == "win" else "❌"
            print(f"[PaperTrader] {emoji} RESOLVED: {order.coin} {order.direction.upper()} "
                  f"→ {winner.upper()} | Maker PnL=${order.pnl_maker:+.2f} "
                  f"Taker PnL=${order.pnl_taker:+.2f} "
                  f"(diff=${order.pnl_maker - order.pnl_taker:+.2f})", flush=True)
        else:
            # No fue llenada — PnL = 0
            order.result = ""
            order.pnl_maker = 0
            order.pnl_taker = 0

        self._add_resolved(order)

    def _add_resolved(self, order: PaperOrder):
        """Agregar orden resuelta al historial."""
        self._resolved.append(order)
        # Mantener máx 500 resueltas
        if len(self._resolved) > 500:
            self._resolved = self._resolved[-500:]

    # ── Consultas de estado ────────────────────────────────────────

    def get_status(self) -> dict:
        """Estado general para el dashboard."""
        total_trades = self._wins + self._losses
        fill_total = self._fills + self._no_fills
        return {
            "enabled": self._enabled,
            "config": self._config,
            "capital": round(self._capital, 2),
            "total_pnl_maker": round(self._total_pnl_maker, 2),
            "total_pnl_taker": round(self._total_pnl_taker, 2),
            "pnl_difference": round(self._total_pnl_maker - self._total_pnl_taker, 2),
            "total_rebates": round(self._total_rebates, 4),
            "wins": self._wins,
            "losses": self._losses,
            "total_trades": total_trades,
            "win_rate": round((self._wins / total_trades * 100), 1) if total_trades > 0 else 0,
            "fills": self._fills,
            "no_fills": self._no_fills,
            "fill_rate": round((self._fills / fill_total * 100), 1) if fill_total > 0 else 0,
            "open_orders": len([o for o in self._orders.values() if o.status == "open"]),
            "filled_pending": len([o for o in self._orders.values() if o.status == "filled"]),
            "roi_pct": round((self._total_pnl_maker / self._config["initial_capital"] * 100), 1)
                       if self._config["initial_capital"] > 0 else 0,
        }

    def get_open_orders(self) -> list[dict]:
        """Órdenes abiertas (en el book simulado)."""
        return [o.to_dict() for o in self._orders.values()
                if o.status in ("open", "filled")]

    def get_resolved_orders(self, limit: int = 100) -> list[dict]:
        """Órdenes resueltas (historial)."""
        return [o.to_dict() for o in reversed(self._resolved[-limit:])]

    def reset(self):
        """Resetear todo el estado del paper trader."""
        self._orders.clear()
        self._resolved.clear()
        self._capital = self._config["initial_capital"]
        self._total_pnl_maker = 0.0
        self._total_pnl_taker = 0.0
        self._total_rebates = 0.0
        self._fills = 0
        self._no_fills = 0
        self._wins = 0
        self._losses = 0
        self._order_counter = 0
        print("[PaperTrader] Estado reseteado", flush=True)
