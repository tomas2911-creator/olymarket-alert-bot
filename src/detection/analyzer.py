"""Anomaly detection and scoring for trades."""
from datetime import datetime, timedelta
from typing import Optional
import structlog

from src.models import Trade, WalletStats, MarketBaseline, AlertCandidate

logger = structlog.get_logger()


class AnomalyAnalyzer:
    """Analyzes trades for suspicious patterns and assigns scores."""
    
    def __init__(self, config: dict):
        """Initialize with configuration."""
        detection = config.get("detection", {})
        scoring = config.get("scoring", {})
        
        # Detection thresholds
        self.fresh_wallet_max_trades = detection.get("fresh_wallet_max_trades", 5)
        self.fresh_wallet_max_days = detection.get("fresh_wallet_max_days", 30)
        self.min_size_usd = detection.get("min_size_usd", 2000)
        self.market_percentile = detection.get("market_percentile", 95)
        self.wallet_size_multiplier = detection.get("wallet_size_multiplier", 5)
        self.concentration_threshold = detection.get("concentration_threshold_pct", 10)
        
        # Scoring
        self.fresh_wallet_points = scoring.get("fresh_wallet_points", 2)
        self.large_size_points = scoring.get("large_size_points", 2)
        self.market_anomaly_points = scoring.get("market_anomaly_points", 2)
        self.wallet_shift_points = scoring.get("wallet_shift_points", 3)
        self.concentration_points = scoring.get("concentration_points", 3)
        self.alert_threshold = scoring.get("alert_threshold", 5)
    
    def analyze(
        self,
        trade: Trade,
        wallet_stats: Optional[WalletStats],
        market_baseline: Optional[MarketBaseline]
    ) -> AlertCandidate:
        """Analyze a trade and return an AlertCandidate with score and triggers."""
        score = 0
        triggers = []
        
        # 1. Check wallet freshness
        if self._is_fresh_wallet(wallet_stats):
            score += self.fresh_wallet_points
            trade_count = wallet_stats.total_trades if wallet_stats else 0
            triggers.append(f"🆕 Fresh wallet ({trade_count} trades)")
        
        # 2. Check absolute size
        if self._is_large_absolute_size(trade):
            score += self.large_size_points
            triggers.append(f"💰 Large size (${trade.size:,.0f})")
        
        # 3. Check size vs market baseline
        if self._is_market_anomaly(trade, market_baseline):
            score += self.market_anomaly_points
            p95 = market_baseline.p95_trade_size if market_baseline else 0
            triggers.append(f"📊 Market anomaly (>${p95:,.0f} p95)")
        
        # 4. Check wallet behavior shift
        if self._is_wallet_shift(trade, wallet_stats):
            score += self.wallet_shift_points
            avg = wallet_stats.avg_trade_size if wallet_stats else 0
            mult = trade.size / avg if avg > 0 else 0
            triggers.append(f"🔄 Behavior shift ({mult:.1f}x avg)")
        
        # 5. Check concentration (if baseline available)
        if self._is_high_concentration(trade, market_baseline):
            score += self.concentration_points
            triggers.append(f"🎯 High concentration")
        
        return AlertCandidate(
            trade=trade,
            wallet_stats=wallet_stats,
            market_baseline=market_baseline,
            score=score,
            triggers=triggers
        )
    
    def should_alert(self, candidate: AlertCandidate) -> bool:
        """Check if candidate meets alert threshold."""
        return candidate.score >= self.alert_threshold
    
    def _is_fresh_wallet(self, wallet_stats: Optional[WalletStats]) -> bool:
        """Check if wallet is fresh (new or few trades)."""
        if wallet_stats is None:
            return True  # Unknown wallet = fresh
        
        # Check trade count
        if wallet_stats.total_trades <= self.fresh_wallet_max_trades:
            return True
        
        # Check age
        days_old = (datetime.now() - wallet_stats.first_seen).days
        if days_old <= self.fresh_wallet_max_days:
            return True
        
        return False
    
    def _is_large_absolute_size(self, trade: Trade) -> bool:
        """Check if trade size exceeds minimum threshold."""
        return trade.size >= self.min_size_usd
    
    def _is_market_anomaly(self, trade: Trade, baseline: Optional[MarketBaseline]) -> bool:
        """Check if trade size is anomalous for the market."""
        if baseline is None:
            return False
        
        # Use p95 by default
        threshold = baseline.p95_trade_size if self.market_percentile == 95 else baseline.p90_trade_size
        return trade.size >= threshold and threshold > 0
    
    def _is_wallet_shift(self, trade: Trade, wallet_stats: Optional[WalletStats]) -> bool:
        """Check if trade is unusually large for this wallet."""
        if wallet_stats is None:
            return False
        
        if wallet_stats.avg_trade_size <= 0:
            return False
        
        multiplier = trade.size / wallet_stats.avg_trade_size
        return multiplier >= self.wallet_size_multiplier
    
    def _is_high_concentration(self, trade: Trade, baseline: Optional[MarketBaseline]) -> bool:
        """Check if trade represents high concentration of market activity."""
        if baseline is None or baseline.total_volume <= 0:
            return False
        
        # Estimate as % of total volume (simplified)
        pct = (trade.size / baseline.total_volume) * 100
        return pct >= self.concentration_threshold
