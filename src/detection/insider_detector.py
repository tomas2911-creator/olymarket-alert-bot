"""Insider Detection — Detectar patrones sospechosos de información privilegiada."""
from datetime import datetime, timezone


class InsiderDetector:
    """Detecta patrones de insider trading en whale trades."""

    # Umbrales de detección
    FRESH_WALLET_MAX_DAYS = 7
    SINGLE_MARKET_PCT = 80  # % de volumen en un solo mercado
    PRE_SPIKE_WINDOW_MIN = 60  # Ventana para correlacionar con spikes
    CLUSTER_WINDOW_MIN = 30  # Ventana para detectar clusters simultáneos
    MIN_TRADE_SIZE = 10000  # Tamaño mín para considerar insider

    def analyze_trade(self, trade_data: dict, context: dict) -> dict:
        """Analizar un whale trade buscando patrones insider.

        trade_data: {wallet_address, size, side, market_id, created_at, ...}
        context: {
            wallet_age_days, wallet_total_trades, wallet_markets_count,
            wallet_market_volume_pct, recent_spike_pct,
            simultaneous_new_wallets, market_question
        }

        Retorna: {probability: 0-100, patterns: [...], flags: [...]}
        """
        probability = 0
        patterns = []
        flags = []

        size = trade_data.get("size", 0)
        if size < self.MIN_TRADE_SIZE:
            return {"probability": 0, "patterns": [], "flags": []}

        # 1. FRESH WALLET + TRADE GRANDE
        wallet_age = context.get("wallet_age_days")
        wallet_trades = context.get("wallet_total_trades", 0)
        if wallet_age is not None and wallet_age <= self.FRESH_WALLET_MAX_DAYS:
            p = 25 if size >= 50000 else 15
            probability += p
            patterns.append("fresh_wallet")
            flags.append(f"🆕 Wallet nueva ({wallet_age}d, {wallet_trades} trades) + ${size:,.0f}")

        # 2. SINGLE-MARKET WHALE
        market_vol_pct = context.get("wallet_market_volume_pct", 0)
        if market_vol_pct >= self.SINGLE_MARKET_PCT and wallet_trades >= 3:
            probability += 20
            patterns.append("single_market")
            flags.append(f"🎯 {market_vol_pct:.0f}% del volumen en 1 mercado")

        # 3. PRE-SPIKE CORRELATION
        spike_pct = context.get("recent_spike_pct", 0)
        if abs(spike_pct) >= 5:
            # Verificar que la dirección del trade coincide con el spike
            side = trade_data.get("side", "")
            spike_aligns = (side == "BUY" and spike_pct > 0) or (side == "SELL" and spike_pct < 0)
            if spike_aligns:
                probability += 25
                patterns.append("pre_spike")
                flags.append(f"📈 Compró antes de spike +{abs(spike_pct):.1f}%")

        # 4. CLUSTER DE WALLETS NUEVAS
        new_wallets_count = context.get("simultaneous_new_wallets", 0)
        if new_wallets_count >= 3:
            probability += 20
            patterns.append("new_wallet_cluster")
            flags.append(f"👥 {new_wallets_count} wallets nuevas en el mismo mercado")

        # 5. TIMING SOSPECHOSO (trade grande cerca de resolución)
        days_to_resolution = context.get("days_to_resolution")
        if days_to_resolution is not None and days_to_resolution <= 2:
            if wallet_age is not None and wallet_age <= 14:
                probability += 15
                patterns.append("late_entry")
                flags.append(f"⏰ Trade grande {days_to_resolution:.0f}d antes de resolución")

        # Clamp a 0-100
        probability = min(100, max(0, probability))

        # Clasificar nivel
        level = "none"
        if probability >= 60:
            level = "high"
        elif probability >= 35:
            level = "medium"
        elif probability >= 15:
            level = "low"

        return {
            "probability": probability,
            "level": level,
            "patterns": patterns,
            "flags": flags,
        }
