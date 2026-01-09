"""Data models for Polymarket Alert Bot."""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Market:
    """Represents a Polymarket market."""
    condition_id: str
    question: str
    slug: str
    icon: Optional[str] = None
    end_date: Optional[datetime] = None
    active: bool = True


@dataclass
class Trade:
    """Represents a single trade on Polymarket."""
    transaction_hash: str
    market_id: str
    market_question: str
    market_slug: str
    wallet_address: str
    side: str  # "YES" or "NO"
    size: float  # USD value
    price: float
    timestamp: datetime
    outcome: str
    
    @property
    def size_usd(self) -> float:
        """Return size in USD."""
        return self.size


@dataclass
class WalletStats:
    """Statistics for a wallet."""
    address: str
    total_trades: int
    first_seen: datetime
    last_seen: datetime
    total_volume: float
    avg_trade_size: float
    
    @property
    def is_fresh(self) -> bool:
        """Check if wallet is fresh (few trades)."""
        return self.total_trades <= 5


@dataclass
class MarketBaseline:
    """Baseline statistics for a market."""
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
    """A trade that might trigger an alert."""
    trade: Trade
    wallet_stats: Optional[WalletStats]
    market_baseline: Optional[MarketBaseline]
    score: int
    triggers: list[str]
    
    @property
    def should_alert(self) -> bool:
        """Check if score meets threshold."""
        return self.score >= 5  # Default threshold
