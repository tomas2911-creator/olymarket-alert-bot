"""Detección de anomalías y scoring para trades."""
from datetime import datetime
from typing import Optional
import structlog

from src.models import Trade, WalletStats, MarketBaseline, AlertCandidate
from src import config

logger = structlog.get_logger()


class AnomalyAnalyzer:
    """Analiza trades buscando patrones sospechosos y asigna scores."""

    def __init__(self):
        self.alert_threshold = config.ALERT_THRESHOLD

    def analyze(
        self,
        trade: Trade,
        wallet_stats: Optional[WalletStats],
        market_baseline: Optional[MarketBaseline],
        cluster_wallets: Optional[list[str]] = None,
    ) -> AlertCandidate:
        score = 0
        triggers: list[str] = []
        days_to_close: Optional[float] = None
        wallet_hr: Optional[float] = None

        # 1. Wallet nueva
        if self._is_fresh_wallet(wallet_stats):
            score += config.FRESH_WALLET_POINTS
            tc = wallet_stats.total_trades if wallet_stats else 0
            triggers.append(f"🆕 Wallet nueva ({tc}tx)")

        # 2. Tamaño absoluto grande
        if trade.size >= config.LARGE_SIZE_USD:
            score += config.LARGE_SIZE_POINTS
            triggers.append(f"💰 Grande ${trade.size:,.0f}")

        # 3. Anomalía vs mercado
        if self._is_market_anomaly(trade, market_baseline):
            score += config.MARKET_ANOMALY_POINTS
            p95 = market_baseline.p95_trade_size if market_baseline else 0
            triggers.append(f"📊 Anomalía mercado")

        # 4. Cambio de comportamiento de wallet
        if self._is_wallet_shift(trade, wallet_stats):
            score += config.WALLET_SHIFT_POINTS
            avg = wallet_stats.avg_trade_size if wallet_stats else 0
            mult = trade.size / avg if avg > 0 else 0
            triggers.append(f"🔄 {mult:.1f}x su promedio")

        # 5. Alta concentración en mercado
        if self._is_high_concentration(trade, market_baseline):
            score += config.CONCENTRATION_POINTS
            triggers.append("🎯 Alta concentración")

        # 6. NUEVO: Proximidad al cierre del mercado
        if trade.market_end_date:
            delta = trade.market_end_date - datetime.now()
            days_to_close = max(delta.total_seconds() / 86400, 0)
            if days_to_close <= 7:
                score += config.TIME_PROXIMITY_POINTS
                if days_to_close < 1:
                    triggers.append(f"⏰ Cierra {days_to_close*24:.0f}h")
                else:
                    triggers.append(f"⏰ Cierra {days_to_close:.0f}d")

        # 7. NUEVO: Clustering de wallets
        if cluster_wallets and len(cluster_wallets) >= 3:
            score += config.CLUSTER_POINTS
            triggers.append(f"👥 Cluster {len(cluster_wallets)}w")

        # 8. NUEVO: Hit rate de la wallet (bonus informativo)
        if wallet_stats and (wallet_stats.win_count + wallet_stats.loss_count) >= 3:
            wallet_hr = wallet_stats.hit_rate
            if wallet_hr >= 70:
                score += 2
            triggers.append(f"🏆 {wallet_hr:.0f}% win")

        return AlertCandidate(
            trade=trade,
            wallet_stats=wallet_stats,
            market_baseline=market_baseline,
            score=score,
            triggers=triggers,
            cluster_wallets=cluster_wallets or [],
            days_to_resolution=days_to_close,
            wallet_hit_rate=wallet_hr,
        )

    def should_alert(self, candidate: AlertCandidate) -> bool:
        return candidate.score >= self.alert_threshold

    def _is_fresh_wallet(self, ws: Optional[WalletStats]) -> bool:
        if ws is None:
            return True
        if ws.total_trades <= config.FRESH_WALLET_MAX_TRADES:
            return True
        days_old = (datetime.now() - ws.first_seen).days
        return days_old <= config.FRESH_WALLET_MAX_DAYS

    def _is_market_anomaly(self, trade: Trade, bl: Optional[MarketBaseline]) -> bool:
        if bl is None:
            return False
        threshold = bl.p95_trade_size if config.MARKET_PERCENTILE == 95 else bl.p90_trade_size
        return trade.size >= threshold > 0

    def _is_wallet_shift(self, trade: Trade, ws: Optional[WalletStats]) -> bool:
        if ws is None or ws.avg_trade_size <= 0:
            return False
        return (trade.size / ws.avg_trade_size) >= config.WALLET_SIZE_MULTIPLIER

    def _is_high_concentration(self, trade: Trade, bl: Optional[MarketBaseline]) -> bool:
        if bl is None or bl.total_volume <= 0:
            return False
        return (trade.size / bl.total_volume) * 100 >= config.CONCENTRATION_THRESHOLD_PCT
