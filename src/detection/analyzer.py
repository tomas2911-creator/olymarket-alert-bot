"""Detección de anomalías y scoring para trades — v4.0 con 14 señales."""
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
        # Nuevos contextos para v4.0
        accumulation_info: Optional[dict] = None,
        market_price: Optional[float] = None,
        smart_cluster_count: int = 0,
        # v5.0: módulos opcionales
        orderbook_depth_pct: Optional[float] = None,
        market_liquidity: Optional[float] = None,
        market_category: Optional[str] = None,
        # v6.0: wallet baskets + sniper dbscan
        wallet_category_shift: bool = False,
        cross_basket_count: int = 0,
        sniper_cluster_size: int = 0,
    ) -> AlertCandidate:
        score = 0
        triggers: list[str] = []
        days_to_close: Optional[float] = None
        wallet_hr: Optional[float] = None

        # ── Señales originales (1-8) ──────────────────────────────────

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
            triggers.append("📊 Anomalía mercado")

        # 4. Cambio de comportamiento de wallet
        if self._is_wallet_shift(trade, wallet_stats):
            score += config.WALLET_SHIFT_POINTS
            avg = wallet_stats.avg_trade_size if wallet_stats else 0
            mult = trade.size / avg if avg > 0 else 0
            triggers.append(f"🔄 {mult:.1f}x promedio")

        # 5. Alta concentración en mercado
        if self._is_high_concentration(trade, market_baseline):
            score += config.CONCENTRATION_POINTS
            triggers.append("🎯 Alta concentración")

        # 6. Proximidad al cierre del mercado
        if trade.market_end_date:
            delta = trade.market_end_date - datetime.now()
            days_to_close = max(delta.total_seconds() / 86400, 0)
            if days_to_close <= 7:
                score += config.TIME_PROXIMITY_POINTS
                if days_to_close < 1:
                    triggers.append(f"⏰ Cierra {days_to_close*24:.0f}h")
                else:
                    triggers.append(f"⏰ Cierra {days_to_close:.0f}d")

        # 7. Clustering de wallets
        if cluster_wallets and len(cluster_wallets) >= 3:
            score += config.CLUSTER_POINTS
            triggers.append(f"👥 Cluster {len(cluster_wallets)}w")

        # 8. Hit rate de la wallet
        if wallet_stats and (wallet_stats.win_count + wallet_stats.loss_count) >= 3:
            wallet_hr = wallet_stats.hit_rate
            if wallet_hr >= 70:
                score += 2
            triggers.append(f"🏆 {wallet_hr:.0f}% win")

        # ── Nuevas señales v4.0 (9-14) ───────────────────────────────

        # 9. CONTRARIAN: smart money apuesta contra el consenso del mercado
        if market_price is not None and market_price > 0:
            is_contrarian = False
            if trade.outcome == "Yes" and trade.side == "BUY" and market_price < 0.20:
                is_contrarian = True  # Compra Yes cuando mercado dice 80%+ No
            elif trade.outcome == "No" and trade.side == "BUY" and market_price > 0.80:
                is_contrarian = True  # Compra No cuando mercado dice 80%+ Yes
            elif trade.outcome == "Yes" and trade.side == "SELL" and market_price > 0.80:
                is_contrarian = True  # Vende Yes cuando mercado dice 80%+ Yes
            if is_contrarian:
                score += 3
                triggers.append(f"🔥 Contrarian (precio {market_price:.0%})")

        # 10. ACUMULACIÓN: wallet comprando repetidamente el mismo outcome
        if accumulation_info and accumulation_info.get("count", 0) >= 2:
            acc_count = accumulation_info["count"]
            acc_total = accumulation_info.get("total_size", 0)
            score += 2
            triggers.append(f"📈 Acumula ({acc_count}x, total ${acc_total:,.0f})")

        # 11. PROVEN WINNER: wallet con historial ganador verificado
        if wallet_stats and (wallet_stats.win_count + wallet_stats.loss_count) >= 5:
            if wallet_stats.hit_rate >= 65:
                score += 3
                triggers.append(f"✅ Ganador probado ({wallet_stats.win_count}W/{wallet_stats.loss_count}L)")

        # 12. MULTI-SMART CONFIRMATION: múltiples wallets inteligentes en el mismo lado
        if smart_cluster_count >= 2:
            score += 3
            triggers.append(f"🧠 {smart_cluster_count} smart wallets mismo lado")

        # 13. LATE INSIDER: trade grande cerca del cierre + wallet nueva
        if days_to_close is not None and days_to_close <= 2 and trade.size >= config.LARGE_SIZE_USD:
            if self._is_fresh_wallet(wallet_stats):
                score += 2
                triggers.append("🕵️ Late insider")

        # 14. EXIT ALERT: smart money vendiendo (SELL)
        if trade.side == "SELL" and wallet_stats:
            resolved = wallet_stats.win_count + wallet_stats.loss_count
            if resolved >= 3 and wallet_stats.hit_rate >= 60:
                score += 2
                triggers.append(f"🚪 Exit (smart money vende)")

        # ── Señales v5.0 (módulos opcionales) ─────────────────────

        # 15. ORDER BOOK DEPTH: trade consume % significativo del book
        if config.FEATURE_ORDERBOOK_DEPTH and orderbook_depth_pct is not None:
            if orderbook_depth_pct >= config.ORDERBOOK_MIN_DEPTH_PCT:
                score += config.ORDERBOOK_DEPTH_POINTS
                triggers.append(f"📕 Consume {orderbook_depth_pct:.1f}% del book")

        # 16. MERCADO NICHO: mercado con poca liquidez = insider info más valiosa
        if config.FEATURE_MARKET_CLASSIFICATION and market_liquidity is not None:
            if market_liquidity < config.NICHE_MAX_LIQUIDITY:
                is_mainstream = (market_category or "").lower() in config.MAINSTREAM_CATEGORIES
                if not is_mainstream:
                    score += config.NICHE_MARKET_POINTS
                    triggers.append(f"🔬 Nicho (liq ${market_liquidity:,.0f})")

        # ── Señales v6.0 (wallet baskets + sniper dbscan) ────────────

        # 17. WALLET BASKET SHIFT: wallet opera fuera de su categoría habitual
        if config.FEATURE_WALLET_BASKETS and wallet_category_shift:
            score += config.BASKET_POINTS
            triggers.append("🔀 Fuera de categoría habitual")
            # Si múltiples wallets fuera de categoría operan aquí = más sospechoso
            if cross_basket_count >= config.BASKET_CROSS_MIN:
                score += 2
                triggers.append(f"🧺 {cross_basket_count} wallets cross-category")

        # 18. SNIPER DBSCAN: cluster de wallets operando en ventana temporal muy estrecha
        if config.FEATURE_SNIPER_DBSCAN and sniper_cluster_size >= config.SNIPER_MIN_CLUSTER_SIZE:
            score += config.SNIPER_POINTS
            triggers.append(f"🎯 Sniper cluster ({sniper_cluster_size}w en <{config.SNIPER_TIME_WINDOW_SEC}s)")

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
