"""Smart Score Mejorado — Scoring profesional de wallets (-100 a +100)."""
import math
from datetime import datetime, timezone
from typing import Optional


class SmartScoreCalculator:
    """Calcula un smart score mejorado basado en múltiples factores."""

    # Pesos de cada componente (suman ~1.0)
    WEIGHT_WINRATE = 0.25
    WEIGHT_CONSISTENCY = 0.20
    WEIGHT_SIZE_WEIGHTED = 0.15
    WEIGHT_RECENCY = 0.15
    WEIGHT_DIVERSIFICATION = 0.10
    WEIGHT_ROI = 0.15

    def calculate(self, wallet_data: dict) -> dict:
        """Calcular smart score mejorado para una wallet.

        wallet_data debe contener:
        - wins, losses, total_trades
        - trades_detail: lista de {pnl, size, created_at, market_id}
        - total_volume, total_pnl
        - markets_traded
        """
        wins = wallet_data.get("wins", 0)
        losses = wallet_data.get("losses", 0)
        total_resolved = wins + losses
        total_trades = wallet_data.get("total_trades", 0)
        trades_detail = wallet_data.get("trades_detail", [])
        total_volume = wallet_data.get("total_volume", 0)
        total_pnl = wallet_data.get("total_pnl", 0)
        markets_traded = wallet_data.get("markets_traded", 0)

        # Si no hay suficientes datos, retornar score neutro
        if total_resolved < 3:
            return {
                "smart_score": 0,
                "consistency_score": 0,
                "is_market_maker": False,
                "components": {},
            }

        # 1. Win Rate Score (-50 a +50)
        winrate = wins / max(total_resolved, 1)
        winrate_score = (winrate - 0.5) * 100  # -50 a +50

        # 2. Consistencia (streak analysis + varianza)
        consistency_score = self._calc_consistency(trades_detail)

        # 3. Size-weighted wins (wins con montos grandes valen más)
        size_weighted_score = self._calc_size_weighted(trades_detail)

        # 4. Recencia (trades recientes pesan más)
        recency_score = self._calc_recency(trades_detail)

        # 5. Diversificación (operar en múltiples mercados = más confiable)
        diversification_score = self._calc_diversification(markets_traded, total_trades)

        # 6. ROI Score
        roi_score = self._calc_roi(total_pnl, total_volume)

        # Score final ponderado (-100 a +100)
        raw_score = (
            winrate_score * self.WEIGHT_WINRATE +
            consistency_score * self.WEIGHT_CONSISTENCY +
            size_weighted_score * self.WEIGHT_SIZE_WEIGHTED +
            recency_score * self.WEIGHT_RECENCY +
            diversification_score * self.WEIGHT_DIVERSIFICATION +
            roi_score * self.WEIGHT_ROI
        )

        # Clamp a -100..+100
        smart_score = max(-100, min(100, round(raw_score)))

        # Detectar market maker: volumen alto pero PnL ~0
        is_mm = self._detect_market_maker(total_volume, total_pnl, total_trades)

        return {
            "smart_score": smart_score,
            "consistency_score": round(consistency_score, 1),
            "is_market_maker": is_mm,
            "components": {
                "winrate": round(winrate_score, 1),
                "consistency": round(consistency_score, 1),
                "size_weighted": round(size_weighted_score, 1),
                "recency": round(recency_score, 1),
                "diversification": round(diversification_score, 1),
                "roi": round(roi_score, 1),
            },
        }

    def _calc_consistency(self, trades: list) -> float:
        """Analizar consistencia de resultados (-50 a +50)."""
        if not trades:
            return 0
        pnls = [t.get("pnl", 0) for t in trades if t.get("pnl") is not None]
        if len(pnls) < 3:
            return 0

        # Streak analysis: rachas ganadoras largas = positivo
        max_win_streak = 0
        max_loss_streak = 0
        current_streak = 0
        for pnl in pnls:
            if pnl > 0:
                current_streak = max(0, current_streak) + 1
                max_win_streak = max(max_win_streak, current_streak)
            elif pnl < 0:
                current_streak = min(0, current_streak) - 1
                max_loss_streak = max(max_loss_streak, abs(current_streak))
            else:
                current_streak = 0

        streak_score = (max_win_streak - max_loss_streak) * 5  # -25 a +25 approx

        # Varianza: baja varianza en PnL = más consistente
        avg_pnl = sum(pnls) / len(pnls)
        variance = sum((p - avg_pnl) ** 2 for p in pnls) / len(pnls)
        std = math.sqrt(variance) if variance > 0 else 0
        # Normalizar: std baja = score alto
        cv = std / max(abs(avg_pnl), 1)  # Coeficiente de variación
        variance_score = max(-25, min(25, 25 - cv * 10))

        return max(-50, min(50, streak_score + variance_score))

    def _calc_size_weighted(self, trades: list) -> float:
        """Wins con montos grandes valen más (-50 a +50)."""
        if not trades:
            return 0
        weighted_sum = 0
        total_size = 0
        for t in trades:
            pnl = t.get("pnl")
            size = t.get("size", 0)
            if pnl is None or size <= 0:
                continue
            win = 1 if pnl > 0 else -1
            weighted_sum += win * size
            total_size += size

        if total_size == 0:
            return 0
        # Normalizar a -50..+50
        ratio = weighted_sum / total_size  # -1 a +1
        return ratio * 50

    def _calc_recency(self, trades: list) -> float:
        """Trades recientes pesan más (-50 a +50). Decay exponencial."""
        if not trades:
            return 0
        now = datetime.now(timezone.utc)
        weighted_wins = 0
        weighted_total = 0
        for t in trades:
            pnl = t.get("pnl")
            if pnl is None:
                continue
            created = t.get("created_at")
            if created:
                if isinstance(created, str):
                    try:
                        created = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    except Exception:
                        continue
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                days_ago = max((now - created).total_seconds() / 86400, 0.1)
            else:
                days_ago = 30  # Default si no hay fecha
            weight = math.exp(-days_ago / 30)  # Decay en 30 días
            if pnl > 0:
                weighted_wins += weight
            weighted_total += weight

        if weighted_total == 0:
            return 0
        ratio = (weighted_wins / weighted_total) - 0.5  # -0.5 a +0.5
        return ratio * 100  # -50 a +50

    def _calc_diversification(self, markets_traded: int, total_trades: int) -> float:
        """Diversificación: operar en múltiples mercados es positivo (-50 a +50)."""
        if total_trades == 0 or markets_traded == 0:
            return 0
        # Ratio mercados/trades: más alto = más diversificado
        ratio = markets_traded / total_trades
        if ratio > 0.8:
            return 30  # Muy diversificado
        elif ratio > 0.5:
            return 20
        elif ratio > 0.3:
            return 10
        elif ratio > 0.1:
            return 0
        else:
            return -20  # Demasiado concentrado

    def _calc_roi(self, total_pnl: float, total_volume: float) -> float:
        """ROI score (-50 a +50)."""
        if total_volume == 0:
            return 0
        roi = total_pnl / total_volume  # -1 a +X
        # Clamp y normalizar
        clamped = max(-0.5, min(0.5, roi))
        return clamped * 100  # -50 a +50

    def _detect_market_maker(self, volume: float, pnl: float, trades: int) -> bool:
        """Detectar market makers: volumen alto, PnL ~0, muchos trades."""
        if trades < 50 or volume < 100000:
            return False
        pnl_ratio = abs(pnl) / max(volume, 1)
        return pnl_ratio < 0.005  # PnL < 0.5% del volumen = market maker
