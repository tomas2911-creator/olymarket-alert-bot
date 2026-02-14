"""Data models for Polymarket Alert Bot."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ── Dataclasses internos ─────────────────────────────────────────────

@dataclass
class Market:
    """Representa un mercado de Polymarket."""
    condition_id: str
    question: str
    slug: str
    icon: Optional[str] = None
    end_date: Optional[datetime] = None
    active: bool = True
    category: Optional[str] = None
    volume_24h: float = 0.0
    liquidity: float = 0.0


@dataclass
class Trade:
    """Representa un trade en Polymarket."""
    transaction_hash: str
    market_id: str
    market_question: str
    market_slug: str
    wallet_address: str
    side: str           # "BUY" / "SELL"
    size: float         # USD value
    price: float
    timestamp: datetime
    outcome: str        # "Yes" / "No"
    market_end_date: Optional[datetime] = None
    market_category: Optional[str] = None

    @property
    def size_usd(self) -> float:
        return self.size


@dataclass
class WalletStats:
    """Estadísticas de una wallet."""
    address: str
    total_trades: int
    first_seen: datetime
    last_seen: datetime
    total_volume: float
    avg_trade_size: float
    markets_traded: int = 0
    win_count: int = 0
    loss_count: int = 0

    @property
    def is_fresh(self) -> bool:
        return self.total_trades <= 5

    @property
    def hit_rate(self) -> float:
        total = self.win_count + self.loss_count
        return (self.win_count / total * 100) if total > 0 else 0.0


@dataclass
class MarketBaseline:
    """Estadísticas base de un mercado."""
    condition_id: str
    trade_count: int
    total_volume: float
    avg_trade_size: float
    median_trade_size: float
    p90_trade_size: float
    p95_trade_size: float
    last_updated: datetime


@dataclass
class AlertCandidate:
    """Un trade que podría disparar alerta."""
    trade: Trade
    wallet_stats: Optional[WalletStats]
    market_baseline: Optional[MarketBaseline]
    score: int
    triggers: list[str] = field(default_factory=list)
    cluster_wallets: list[str] = field(default_factory=list)
    days_to_resolution: Optional[float] = None
    wallet_hit_rate: Optional[float] = None

    @property
    def should_alert(self) -> bool:
        return self.score >= 5
