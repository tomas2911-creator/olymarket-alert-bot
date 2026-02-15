"""Bankroll Tracking — Trackear balance real y métricas de riesgo."""
import time
import httpx
from src import config


class BankrollTracker:
    """Trackea el balance USDC real y calcula métricas de riesgo."""

    def __init__(self, db):
        self.db = db
        self._balance = config.BANKROLL_INITIAL
        self._peak_balance = config.BANKROLL_INITIAL
        self._daily_pnl = 0.0
        self._history = []  # [(timestamp, balance)]
        self._last_check = 0
        self._trades_total = 0
        self._trades_won = 0

    async def update_balance(self):
        """Actualizar balance desde on-chain si hay wallet configurada."""
        if not config.FEATURE_BANKROLL:
            return

        now = time.time()
        if now - self._last_check < 300:  # Check cada 5 min
            return
        self._last_check = now

        addr = config.BANKROLL_WALLET_ADDRESS
        if addr:
            try:
                balance = await self._get_usdc_balance(addr)
                if balance is not None:
                    self._balance = balance
            except Exception as e:
                print(f"Error obteniendo balance: {e}", flush=True)

        # Actualizar peak
        if self._balance > self._peak_balance:
            self._peak_balance = self._balance

        # Registrar en historial
        self._history.append((now, self._balance))
        # Mantener solo últimos 30 días
        cutoff = now - 86400 * 30
        self._history = [(t, b) for t, b in self._history if t > cutoff]

        # Guardar en DB
        try:
            await self.db.log_bankroll(self._balance, self._daily_pnl)
        except Exception:
            pass

    async def _get_usdc_balance(self, address: str) -> float:
        """Obtener balance USDC de una wallet en Polygon."""
        usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC en Polygon
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                if config.POLYGONSCAN_API_KEY:
                    r = await client.get(
                        "https://api.polygonscan.com/api",
                        params={
                            "module": "account",
                            "action": "tokenbalance",
                            "contractaddress": usdc_contract,
                            "address": address,
                            "tag": "latest",
                            "apikey": config.POLYGONSCAN_API_KEY,
                        }
                    )
                    if r.status_code == 200:
                        data = r.json()
                        if data.get("status") == "1":
                            # USDC tiene 6 decimales
                            return int(data["result"]) / 1_000_000
        except Exception:
            pass
        return None

    def record_trade(self, pnl: float, won: bool):
        """Registrar resultado de un trade."""
        self._daily_pnl += pnl
        self._balance += pnl
        self._trades_total += 1
        if won:
            self._trades_won += 1

    def can_trade(self, bet_size: float) -> bool:
        """Verificar si se puede tradear según límites de bankroll."""
        if not config.FEATURE_BANKROLL:
            return True
        max_bet = self._balance * (config.BANKROLL_MAX_SINGLE_BET_PCT / 100)
        return bet_size <= max_bet and self._balance > bet_size

    def get_max_bet(self) -> float:
        """Obtener el tamaño máximo de apuesta permitido."""
        if not config.FEATURE_BANKROLL:
            return 999999
        return self._balance * (config.BANKROLL_MAX_SINGLE_BET_PCT / 100)

    def get_stats(self) -> dict:
        drawdown = 0
        if self._peak_balance > 0:
            drawdown = ((self._peak_balance - self._balance) / self._peak_balance) * 100

        roi = 0
        if config.BANKROLL_INITIAL > 0:
            roi = ((self._balance - config.BANKROLL_INITIAL) / config.BANKROLL_INITIAL) * 100

        win_rate = 0
        if self._trades_total > 0:
            win_rate = (self._trades_won / self._trades_total) * 100

        return {
            "enabled": config.FEATURE_BANKROLL,
            "balance": round(self._balance, 2),
            "initial": config.BANKROLL_INITIAL,
            "roi_pct": round(roi, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "peak_balance": round(self._peak_balance, 2),
            "max_drawdown_pct": round(drawdown, 2),
            "trades_total": self._trades_total,
            "win_rate_pct": round(win_rate, 1),
            "max_bet": round(self.get_max_bet(), 2),
            "history": [(t, round(b, 2)) for t, b in self._history[-100:]],
        }
