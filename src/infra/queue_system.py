"""Queue System — Separar detección de ejecución con asyncio.Queue."""
import asyncio
import time
from src import config


class SignalQueue:
    """Cola de señales para procesamiento asíncrono."""

    def __init__(self):
        self._queue = asyncio.Queue(maxsize=config.QUEUE_MAX_SIZE)
        self._processed = 0
        self._dropped = 0
        self._workers_running = False

    async def put(self, signal: dict):
        """Agregar señal a la cola."""
        if not config.FEATURE_QUEUE:
            return False
        try:
            signal["queued_at"] = time.time()
            self._queue.put_nowait(signal)
            return True
        except asyncio.QueueFull:
            self._dropped += 1
            print(f"Queue llena, señal descartada (dropped: {self._dropped})", flush=True)
            return False

    async def start_workers(self, handler):
        """Iniciar workers que procesan señales de la cola."""
        if not config.FEATURE_QUEUE:
            return
        self._workers_running = True
        for i in range(config.QUEUE_WORKERS):
            asyncio.create_task(self._worker(i, handler))
        print(f"Queue: {config.QUEUE_WORKERS} workers iniciados", flush=True)

    async def _worker(self, worker_id: int, handler):
        """Worker que procesa señales de la cola."""
        while self._workers_running:
            try:
                signal = await asyncio.wait_for(self._queue.get(), timeout=5.0)
                latency = time.time() - signal.get("queued_at", time.time())
                try:
                    await handler(signal)
                    self._processed += 1
                except Exception as e:
                    print(f"Queue worker {worker_id} error: {e}", flush=True)
                finally:
                    self._queue.task_done()
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"Queue worker {worker_id} fatal: {e}", flush=True)
                await asyncio.sleep(1)

    async def stop(self):
        """Detener workers."""
        self._workers_running = False

    def get_stats(self) -> dict:
        return {
            "enabled": config.FEATURE_QUEUE,
            "pending": self._queue.qsize(),
            "processed": self._processed,
            "dropped": self._dropped,
            "max_size": config.QUEUE_MAX_SIZE,
            "workers": config.QUEUE_WORKERS,
        }
