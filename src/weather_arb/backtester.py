"""Backtester / Paper Trader para weather arb.

Tracking de PnL real basado en señales generadas por el detector.
Cada señal se registra como un "paper trade" y se resuelve cuando
el mercado de Polymarket cierra, usando la resolución real.

NO es un backtest histórico como el crypto — es tracking en vivo
de las alertas con PnL real calculado sobre resoluciones reales.
"""
import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog
import httpx

from src import config

logger = structlog.get_logger()

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


@dataclass
class PaperTrade:
    """Un trade simulado (paper) del weather arb."""
    city: str
    city_name: str
    date: str
    range_label: str
    condition_id: str
    event_slug: str
    entry_odds: float            # Precio al que "compramos" (poly_odds al momento)
    ensemble_prob: float         # Probabilidad del ensemble al momento
    edge_pct: float              # Edge al momento de la señal
    confidence: float            # Confianza del ensemble
    bet_size: float              # Monto simulado
    unit: str                    # °F o °C
    strategy: str = "conviction"  # "conviction" (BUY YES) o "elimination" (BUY NO)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved: bool = False
    result: Optional[str] = None  # "win" o "loss"
    pnl: float = 0.0
    resolution_source: str = ""
    actual_temp: Optional[float] = None  # Temperatura real registrada
    current_odds: Optional[float] = None  # Odds actuales en vivo
    unrealized_pnl: Optional[float] = None  # PnL no realizado
    last_price_update: Optional[float] = None  # Timestamp última actualización

    def to_dict(self) -> dict:
        return {
            "city": self.city,
            "city_name": self.city_name,
            "date": self.date,
            "range_label": self.range_label,
            "condition_id": self.condition_id,
            "event_slug": self.event_slug,
            "entry_odds": round(self.entry_odds, 4),
            "ensemble_prob": round(self.ensemble_prob, 4),
            "edge_pct": round(self.edge_pct, 2),
            "confidence": round(self.confidence, 1),
            "bet_size": self.bet_size,
            "unit": self.unit,
            "strategy": self.strategy,
            "created_at": self.created_at.isoformat() if isinstance(self.created_at, datetime) else str(self.created_at),
            "resolved": self.resolved,
            "result": self.result,
            "pnl": round(self.pnl, 2) if self.resolved else None,
            "actual_temp": self.actual_temp,
            "current_odds": round(self.current_odds, 4) if self.current_odds is not None else None,
            "unrealized_pnl": round(self.unrealized_pnl, 2) if self.unrealized_pnl is not None else None,
        }


@dataclass
class PaperTradeStats:
    """Estadísticas agregadas del paper trading."""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    pending: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    avg_edge: float = 0.0
    avg_confidence: float = 0.0
    total_invested: float = 0.0
    roi_pct: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    unrealized_pnl: float = 0.0
    by_city: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "pending": self.pending,
            "total_pnl": round(self.total_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "win_rate": round(self.win_rate, 1),
            "avg_edge": round(self.avg_edge, 1),
            "avg_confidence": round(self.avg_confidence, 1),
            "total_invested": round(self.total_invested, 2),
            "roi_pct": round(self.roi_pct, 1),
            "best_trade": round(self.best_trade, 2),
            "worst_trade": round(self.worst_trade, 2),
            "by_city": self.by_city,
        }


class WeatherPaperTrader:
    """Paper trader: registra señales como trades simulados y resuelve con datos reales."""

    def __init__(self, bet_size: float = 10.0, db=None):
        self.bet_size = bet_size
        self._db = db
        self._trades: list[PaperTrade] = []
        self._resolved_cids: set[str] = set()  # Para evitar resolver dos veces

    async def load_from_db(self, user_id: int = 1):
        """Cargar paper trades persistentes desde la DB al iniciar."""
        if not self._db:
            return
        try:
            rows = await self._db.load_weather_paper_trades(user_id=user_id)
            loaded = 0
            for r in rows:
                cid = r.get("condition_id", "")
                if not cid or any(t.condition_id == cid for t in self._trades):
                    continue
                created = r.get("created_at")
                if isinstance(created, str):
                    try:
                        created = datetime.fromisoformat(created)
                    except Exception:
                        created = datetime.now(timezone.utc)
                trade = PaperTrade(
                    city=r.get("city", ""),
                    city_name=r.get("city_name", ""),
                    date=r.get("date", ""),
                    range_label=r.get("range_label", ""),
                    condition_id=cid,
                    event_slug=r.get("event_slug", ""),
                    entry_odds=float(r.get("entry_odds", 0) or 0),
                    ensemble_prob=float(r.get("ensemble_prob", 0) or 0),
                    edge_pct=float(r.get("edge_pct", 0) or 0),
                    confidence=float(r.get("confidence", 0) or 0),
                    bet_size=float(r.get("bet_size", 0) or 0),
                    unit=r.get("unit", ""),
                    strategy=r.get("strategy", "conviction"),
                    resolution_source=r.get("resolution_source", ""),
                    created_at=created,
                    resolved=bool(r.get("resolved", False)),
                    result=r.get("result"),
                    pnl=float(r.get("pnl", 0) or 0),
                    actual_temp=float(r["actual_temp"]) if r.get("actual_temp") is not None else None,
                    current_odds=float(r["current_odds"]) if r.get("current_odds") is not None else None,
                    unrealized_pnl=float(r["unrealized_pnl"]) if r.get("unrealized_pnl") is not None else None,
                )
                if trade.resolved:
                    self._resolved_cids.add(cid)
                self._trades.append(trade)
                loaded += 1
            if loaded:
                print(f"[WeatherPaper] Cargados {loaded} paper trades desde DB "
                      f"({sum(1 for t in self._trades if t.resolved)} resueltos, "
                      f"{sum(1 for t in self._trades if not t.resolved)} pendientes)", flush=True)
        except Exception as e:
            print(f"[WeatherPaper] Error cargando desde DB: {e}", flush=True)

    def record_signal(self, signal: dict):
        """Registrar una señal del detector como paper trade."""
        cid = signal.get("condition_id", "")
        if not cid:
            return

        # No duplicar
        if any(t.condition_id == cid for t in self._trades):
            return

        trade = PaperTrade(
            city=signal.get("city", ""),
            city_name=signal.get("city_name", ""),
            date=signal.get("date", ""),
            range_label=signal.get("range_label", ""),
            condition_id=cid,
            event_slug=signal.get("event_slug", ""),
            entry_odds=signal.get("poly_odds", 0.5),
            ensemble_prob=signal.get("ensemble_prob", 0),
            edge_pct=signal.get("edge_pct", 0),
            confidence=signal.get("confidence", 0),
            bet_size=self.bet_size,
            unit=signal.get("unit", ""),
            strategy=signal.get("strategy", "conviction"),
            resolution_source=signal.get("resolution_source", ""),
        )
        self._trades.append(trade)
        print(f"[WeatherPaper] \U0001f4dd Paper trade: {trade.city_name} {trade.date} "
              f"\u2192 {trade.range_label} @ {trade.entry_odds:.2f} "
              f"(edge={trade.edge_pct:.1f}%)", flush=True)
        # Persistir en DB
        if self._db:
            asyncio.ensure_future(self._persist_new_trade(trade))

    async def _persist_new_trade(self, trade: PaperTrade):
        """Guardar trade nuevo en DB (fire-and-forget)."""
        try:
            await self._db.insert_weather_paper_trade({
                "condition_id": trade.condition_id,
                "city": trade.city,
                "city_name": trade.city_name,
                "date": trade.date,
                "range_label": trade.range_label,
                "event_slug": trade.event_slug,
                "entry_odds": trade.entry_odds,
                "ensemble_prob": trade.ensemble_prob,
                "edge_pct": trade.edge_pct,
                "confidence": trade.confidence,
                "bet_size": trade.bet_size,
                "unit": trade.unit,
                "strategy": trade.strategy,
                "resolution_source": trade.resolution_source,
                "created_at": trade.created_at,
            })
        except Exception as e:
            print(f"[WeatherPaper] Error persistiendo trade: {e}", flush=True)

    async def resolve_pending(self):
        """Intentar resolver trades pendientes consultando Polymarket."""
        pending = [t for t in self._trades if not t.resolved and t.condition_id not in self._resolved_cids]
        if not pending:
            return

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for trade in pending:
                    try:
                        # Consultar CLOB para ver si mercado cerró
                        resp = await client.get(f"{CLOB_API}/markets/{trade.condition_id}")
                        if resp.status_code != 200:
                            continue
                        data = resp.json()
                        if not data.get("closed"):
                            continue

                        # Buscar ganador
                        winning = None
                        for tok in data.get("tokens", []):
                            if tok.get("winner") is True:
                                winning = tok.get("outcome", "")
                                break
                            try:
                                if float(tok.get("price", 0) or 0) >= 0.95:
                                    winning = tok.get("outcome", "")
                                    break
                            except (ValueError, TypeError):
                                pass

                        if not winning:
                            # Fallback Gamma
                            try:
                                resp2 = await client.get(
                                    f"{GAMMA_API}/events",
                                    params={"slug": trade.event_slug})
                                if resp2.status_code == 200:
                                    events = resp2.json()
                                    if events and events[0].get("closed"):
                                        for m in events[0].get("markets", []):
                                            if m.get("conditionId") == trade.condition_id:
                                                op = m.get("outcomePrices")
                                                if op:
                                                    prices = json.loads(op) if isinstance(op, str) else op
                                                    if float(prices[0]) >= 0.90:
                                                        winning = "Yes"
                                                    elif float(prices[1]) >= 0.90:
                                                        winning = "No"
                            except Exception:
                                pass

                        if not winning:
                            continue

                        # Calcular PnL según strategy
                        is_elim = trade.strategy == "elimination" or trade.range_label.startswith("ELIM:")
                        if is_elim:
                            # Compramos NO: ganamos si outcome es NO
                            won = winning.lower() == "no"
                        else:
                            # Compramos YES: ganamos si outcome es YES
                            won = winning.lower() == "yes"

                        if won:
                            # Cada share paga $1. Shares = bet_size / entry_odds
                            shares = trade.bet_size / trade.entry_odds if trade.entry_odds > 0 else 0
                            trade.pnl = round(shares - trade.bet_size, 2)
                            trade.result = "win"
                        else:
                            trade.pnl = -trade.bet_size
                            trade.result = "loss"

                        trade.resolved = True
                        self._resolved_cids.add(trade.condition_id)

                        emoji = "\u2705" if won else "\u274c"
                        print(f"[WeatherPaper] {emoji} {trade.city_name} {trade.date} "
                              f"{trade.range_label} \u2192 {trade.result.upper()} "
                              f"PnL=${trade.pnl:.2f}", flush=True)

                        # Persistir resolución en DB
                        if self._db:
                            try:
                                await self._db.update_weather_paper_trade(trade.condition_id, {
                                    "resolved": True,
                                    "result": trade.result,
                                    "pnl": trade.pnl,
                                })
                            except Exception:
                                pass

                    except Exception as e:
                        logger.warning("paper_resolve_error", cid=trade.condition_id[:16], error=str(e))

        except Exception as e:
            print(f"[WeatherPaper] Error resolviendo: {e}", flush=True)

    async def update_live_prices(self):
        """Actualizar odds en vivo y PnL no realizado para trades pendientes."""
        pending = [t for t in self._trades if not t.resolved]
        if not pending:
            return
        now = time.time()
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                for trade in pending:
                    # Throttle: no actualizar más de 1 vez por minuto
                    if trade.last_price_update and (now - trade.last_price_update) < 60:
                        continue
                    try:
                        resp = await client.get(f"{CLOB_API}/markets/{trade.condition_id}")
                        if resp.status_code != 200:
                            continue
                        data = resp.json()
                        tokens = data.get("tokens", [])
                        # Buscar token correcto según strategy
                        is_elim = trade.strategy == "elimination" or trade.range_label.startswith("ELIM:")
                        target_outcome = "no" if is_elim else "yes"
                        for tok in tokens:
                            if tok.get("outcome", "").lower() == target_outcome:
                                try:
                                    cur_price = float(tok.get("price", 0) or 0)
                                    trade.current_odds = cur_price
                                    # PnL = valor_actual - inversión
                                    # shares = bet_size / entry_odds
                                    if trade.entry_odds > 0:
                                        shares = trade.bet_size / trade.entry_odds
                                        trade.unrealized_pnl = round((shares * cur_price) - trade.bet_size, 2)
                                    trade.last_price_update = now
                                    # Persistir precios en DB
                                    if self._db:
                                        try:
                                            await self._db.update_weather_paper_trade(trade.condition_id, {
                                                "current_odds": cur_price,
                                                "unrealized_pnl": trade.unrealized_pnl,
                                            })
                                        except Exception:
                                            pass
                                except (ValueError, TypeError):
                                    pass
                                break
                    except Exception:
                        continue
        except Exception as e:
            print(f"[WeatherPaper] Error actualizando precios: {e}", flush=True)

    def get_trades(self, limit: int = 200) -> list[dict]:
        """Obtener trades como dicts para API."""
        sorted_trades = sorted(self._trades, key=lambda t: t.created_at, reverse=True)
        return [t.to_dict() for t in sorted_trades[:limit]]

    def get_stats(self) -> dict:
        """Estadísticas agregadas."""
        stats = PaperTradeStats()
        stats.total_trades = len(self._trades)
        stats.pending = sum(1 for t in self._trades if not t.resolved)

        resolved = [t for t in self._trades if t.resolved]
        stats.wins = sum(1 for t in resolved if t.result == "win")
        stats.losses = sum(1 for t in resolved if t.result == "loss")
        stats.total_pnl = sum(t.pnl for t in resolved)
        stats.total_invested = sum(t.bet_size for t in resolved)

        if resolved:
            stats.win_rate = (stats.wins / len(resolved)) * 100
            stats.best_trade = max(t.pnl for t in resolved)
            stats.worst_trade = min(t.pnl for t in resolved)

        if stats.total_invested > 0:
            stats.roi_pct = (stats.total_pnl / stats.total_invested) * 100

        # PnL no realizado de trades pendientes
        pending_trades = [t for t in self._trades if not t.resolved]
        stats.unrealized_pnl = sum(t.unrealized_pnl or 0 for t in pending_trades)

        if self._trades:
            stats.avg_edge = sum(t.edge_pct for t in self._trades) / len(self._trades)
            stats.avg_confidence = sum(t.confidence for t in self._trades) / len(self._trades)

        # Por ciudad
        by_city = {}
        for t in resolved:
            city = t.city_name
            if city not in by_city:
                by_city[city] = {"wins": 0, "losses": 0, "pnl": 0.0}
            if t.result == "win":
                by_city[city]["wins"] += 1
            else:
                by_city[city]["losses"] += 1
            by_city[city]["pnl"] += t.pnl
        # Redondear
        for city_data in by_city.values():
            city_data["pnl"] = round(city_data["pnl"], 2)
        stats.by_city = by_city

        return stats.to_dict()

    def clear(self):
        """Limpiar todos los trades en memoria (DB se limpia aparte)."""
        self._trades = []
        self._resolved_cids = set()

    async def reset(self, user_id: int = 1) -> int:
        """Reiniciar paper trading: borrar memoria + DB."""
        count = len(self._trades)
        self.clear()
        if self._db:
            try:
                await self._db.clear_weather_paper_trades(user_id=user_id)
            except Exception as e:
                print(f"[WeatherPaper] Error limpiando DB: {e}", flush=True)
        print(f"[WeatherPaper] Paper trading reiniciado. {count} trades borrados.", flush=True)
        return count
