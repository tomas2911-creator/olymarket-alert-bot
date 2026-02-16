"""Alert Backtester — v10.0

Simula qué habría pasado si se hubieran ejecutado copy-trades en alertas históricas.
Usa datos reales de price impact (price_1h, price_6h, price_24h, resolution) para
calcular PnL simulado con diferentes estrategias y parámetros.

Permite:
- Optimizar min_score, bet_size, TP/SL
- Comparar rendimiento por categoría
- Validar Kelly Criterion con datos reales
- Estimar drawdown máximo
"""
import structlog
from datetime import datetime, timezone
from typing import Optional

logger = structlog.get_logger()


class AlertBacktester:
    """Backtester para alertas de copy-trading."""

    def __init__(self, db):
        self.db = db

    async def run_backtest(self, params: dict) -> dict:
        """Ejecutar backtest con parámetros dados.
        
        params:
            days: int (default 30)
            min_score: int (default 7)
            bet_size: float (default 10)
            take_profit_pct: float (default 25)
            stop_loss_pct: float (default 15)
            min_hit_rate: float (default 0)
            excluded_categories: list[str] (default [])
            kelly_enabled: bool (default False)
            max_positions: int (default 5)
        """
        days = params.get("days", 30)
        min_score = params.get("min_score", 7)
        bet_size = params.get("bet_size", 10.0)
        tp_pct = params.get("take_profit_pct", 25.0)
        sl_pct = params.get("stop_loss_pct", 15.0)
        min_hit_rate = params.get("min_hit_rate", 0)
        excluded_cats = set(c.lower() for c in params.get("excluded_categories", []))
        kelly = params.get("kelly_enabled", False)
        max_pos = params.get("max_positions", 5)

        # Obtener datos históricos
        alerts = await self.db.get_backtest_data(days=days, min_score=min_score)
        if not alerts:
            return {"error": "Sin datos para backtest", "trades": 0}

        # Simular trades
        trades = []
        balance = 1000.0  # Balance inicial simulado
        peak_balance = balance
        max_drawdown = 0
        open_positions = 0
        daily_trades = {}
        wins = 0
        losses = 0
        total_pnl = 0
        by_category = {}
        by_score = {}

        for alert in alerts:
            entry = alert.get("entry_price", 0)
            score = alert.get("score", 0)
            category = (alert.get("category") or "").lower()
            hit_rate = alert.get("hit_rate", 0)
            sim_result = alert.get("sim_result", "open")

            # Filtros
            if score < min_score:
                continue
            if min_hit_rate > 0 and hit_rate < min_hit_rate:
                continue
            if category and category in excluded_cats:
                continue
            if sim_result == "open":
                continue  # Sin resolución, no se puede backtestear
            if entry <= 0 or entry >= 1:
                continue

            # Calcular size
            size = bet_size
            if kelly and entry > 0:
                # Kelly simplificado
                b = (1.0 / entry) - 1
                p = min(0.5 + (score / 30) * 0.3, 0.85)
                k = (p * b - (1 - p)) / b if b > 0 else 0
                k = max(k, 0) * 0.25
                size = bet_size * max(0.2, min(k / 0.25, 2.0))

            # Simular resultado
            exit_price = alert.get("exit_price")
            if exit_price is None:
                continue

            # TP/SL simulation
            pnl_pct = (exit_price - entry) / entry * 100
            actual_pnl_pct = pnl_pct

            # Simular TP/SL (aproximado usando price checkpoints)
            price_1h = alert.get("price_1h")
            price_6h = alert.get("price_6h")
            price_24h = alert.get("price_24h")

            hit_tp = False
            hit_sl = False

            for checkpoint_price in [price_1h, price_6h, price_24h]:
                if checkpoint_price and checkpoint_price > 0 and entry > 0:
                    cp_pct = (checkpoint_price - entry) / entry * 100
                    if tp_pct > 0 and cp_pct >= tp_pct and not hit_tp:
                        actual_pnl_pct = tp_pct
                        hit_tp = True
                        break
                    if sl_pct > 0 and cp_pct <= -sl_pct and not hit_sl:
                        actual_pnl_pct = -sl_pct
                        hit_sl = True
                        break

            # Si no se tocó TP/SL, usar el resultado final
            if not hit_tp and not hit_sl:
                actual_pnl_pct = pnl_pct

            pnl_usd = size * (actual_pnl_pct / 100)
            balance += pnl_usd
            total_pnl += pnl_usd

            if balance > peak_balance:
                peak_balance = balance
            dd = (peak_balance - balance) / peak_balance * 100 if peak_balance > 0 else 0
            if dd > max_drawdown:
                max_drawdown = dd

            is_win = pnl_usd > 0
            if is_win:
                wins += 1
            else:
                losses += 1

            # Stats por categoría
            if category:
                if category not in by_category:
                    by_category[category] = {"wins": 0, "losses": 0, "pnl": 0, "trades": 0}
                by_category[category]["trades"] += 1
                by_category[category]["pnl"] += pnl_usd
                if is_win:
                    by_category[category]["wins"] += 1
                else:
                    by_category[category]["losses"] += 1

            # Stats por score bracket
            bracket = f"{(score // 3) * 3}-{(score // 3) * 3 + 2}"
            if bracket not in by_score:
                by_score[bracket] = {"wins": 0, "losses": 0, "pnl": 0, "trades": 0}
            by_score[bracket]["trades"] += 1
            by_score[bracket]["pnl"] += pnl_usd
            if is_win:
                by_score[bracket]["wins"] += 1
            else:
                by_score[bracket]["losses"] += 1

            trades.append({
                "score": score,
                "entry": round(entry, 3),
                "exit": round(exit_price, 3) if exit_price else None,
                "pnl_pct": round(actual_pnl_pct, 1),
                "pnl_usd": round(pnl_usd, 2),
                "category": category,
                "result": "win" if is_win else "loss",
                "tp_hit": hit_tp,
                "sl_hit": hit_sl,
            })

        total = wins + losses
        # Calcular win rate por categoría
        cat_results = []
        for cat, data in sorted(by_category.items(), key=lambda x: x[1]["pnl"], reverse=True):
            cat_total = data["wins"] + data["losses"]
            cat_results.append({
                "category": cat,
                "trades": data["trades"],
                "wins": data["wins"],
                "win_rate": round(data["wins"] / cat_total * 100, 1) if cat_total else 0,
                "pnl": round(data["pnl"], 2),
            })

        score_results = []
        for bracket, data in sorted(by_score.items()):
            s_total = data["wins"] + data["losses"]
            score_results.append({
                "score_range": bracket,
                "trades": data["trades"],
                "wins": data["wins"],
                "win_rate": round(data["wins"] / s_total * 100, 1) if s_total else 0,
                "pnl": round(data["pnl"], 2),
            })

        return {
            "params": {
                "days": days, "min_score": min_score, "bet_size": bet_size,
                "tp_pct": tp_pct, "sl_pct": sl_pct, "kelly": kelly,
            },
            "summary": {
                "total_trades": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / total * 100, 1) if total else 0,
                "total_pnl": round(total_pnl, 2),
                "avg_pnl_per_trade": round(total_pnl / total, 2) if total else 0,
                "final_balance": round(balance, 2),
                "max_drawdown_pct": round(max_drawdown, 1),
                "profit_factor": round(
                    sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0) /
                    abs(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] < 0) or 1), 2
                ),
                "sharpe_estimate": self._calc_sharpe(trades),
            },
            "by_category": cat_results,
            "by_score": score_results,
            "trades": trades[-50:],  # Últimos 50 trades
        }

    def _calc_sharpe(self, trades: list) -> float:
        """Calcular Sharpe ratio aproximado."""
        if len(trades) < 5:
            return 0
        returns = [t["pnl_pct"] for t in trades]
        avg = sum(returns) / len(returns)
        variance = sum((r - avg) ** 2 for r in returns) / len(returns)
        std = variance ** 0.5
        if std <= 0:
            return 0
        return round(avg / std, 2)
