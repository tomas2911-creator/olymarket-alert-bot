"""Rate Limiter inteligente con backoff exponencial."""
import asyncio
import time
from src import config


class RateLimiter:
    """Control de rate limiting para APIs externas con backoff exponencial."""

    def __init__(self):
        self._requests = []  # timestamps de requests recientes
        self._backoff_until = 0  # timestamp hasta el cual esperar
        self._consecutive_errors = 0

    async def acquire(self):
        """Esperar hasta que sea seguro hacer un request."""
        if not config.FEATURE_RATE_LIMITING:
            return

        now = time.time()

        # Backoff activo
        if now < self._backoff_until:
            wait = self._backoff_until - now
            await asyncio.sleep(wait)

        # Limpiar requests viejos (> 60 seg)
        self._requests = [t for t in self._requests if now - t < 60]

        # Esperar si estamos en el límite
        while len(self._requests) >= config.RATE_LIMIT_MAX_PER_MIN:
            await asyncio.sleep(0.5)
            now = time.time()
            self._requests = [t for t in self._requests if now - t < 60]

        self._requests.append(time.time())

    def report_success(self):
        """Reportar request exitoso — resetear backoff."""
        self._consecutive_errors = 0

    def report_error(self, status_code: int = 0):
        """Reportar error — activar backoff exponencial si es rate limit."""
        if status_code == 429 or status_code >= 500:
            self._consecutive_errors += 1
            delay = config.RATE_LIMIT_BACKOFF_BASE ** self._consecutive_errors
            delay = min(delay, 60)  # Máximo 60 segundos de espera
            self._backoff_until = time.time() + delay
            print(f"Rate limiter: backoff {delay:.1f}s (error #{self._consecutive_errors})", flush=True)

    def get_stats(self) -> dict:
        now = time.time()
        recent = len([t for t in self._requests if now - t < 60])
        return {
            "enabled": config.FEATURE_RATE_LIMITING,
            "requests_last_min": recent,
            "max_per_min": config.RATE_LIMIT_MAX_PER_MIN,
            "consecutive_errors": self._consecutive_errors,
            "backoff_active": now < self._backoff_until,
        }


# Instancia global
_limiter = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter
