"""Whale Scanner — Detección híbrida de whale trades.

Combina dos estrategias:
1. Agregación de fills: acumula fills pequeños del endpoint /trades por wallet+mercado
   y detecta cuando la suma supera el umbral de whale.
2. Monitoreo de actividad: consulta /activity de las top wallets conocidas para
   capturar trades grandes que los fills individuales no muestran.
"""
import asyncio
import time
import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.models import Trade
from src import config

_POLY_DATA_API = "https://data-api.polymarket.com"


class WhaleScanner:
    """Scanner híbrido de whale trades."""

    def __init__(self, db):
        self.db = db
        # Agregación de fills: {(wallet, market_id) -> {total_usd, side, last_ts, ...}}
        self._fill_buffer: dict[tuple[str, str], dict] = {}
        self._fill_buffer_lock = asyncio.Lock()
        # Ventana de agregación en segundos (fills del mismo wallet+market dentro de esta ventana se suman)
        self.aggregation_window_sec = 300  # 5 minutos
        # Umbral mínimo para considerar whale trade (USD)
        self.min_whale_size = config.WHALE_TRACKER_MIN_SIZE
        # Cache de wallets conocidas para monitoreo de actividad
        self._known_wallets: list[str] = []
        self._last_wallet_refresh = 0
        self._wallet_refresh_interval = 600  # Refrescar lista cada 10 min
        # Timestamps del último trade visto por wallet (para no duplicar)
        self._wallet_last_seen: dict[str, float] = {}
        # Intervalo del loop de actividad
        self.activity_scan_interval = 300  # 5 minutos
        # Stats
        self.stats = {
            "fills_aggregated": 0,
            "whales_from_fills": 0,
            "whales_from_activity": 0,
            "wallets_monitored": 0,
            "last_scan_time": None,
        }

    # ── Parte 1: Agregación de fills ──────────────────────────────────

    async def process_fills(self, trades: list[Trade]) -> list[Trade]:
        """Procesar fills del polling y detectar whale trades por agregación.

        Recibe los trades raw del ciclo de polling (ya parseados).
        Retorna lista de whale trades detectados por agregación.
        """
        if not trades:
            return []

        now = time.time()
        detected = []

        async with self._fill_buffer_lock:
            # Limpiar buffer viejo
            expired_keys = [
                k for k, v in self._fill_buffer.items()
                if now - v["last_ts"] > self.aggregation_window_sec * 2
            ]
            for k in expired_keys:
                del self._fill_buffer[k]

            for t in trades:
                if t.size < 1:  # Ignorar fills insignificantes
                    continue

                key = (t.wallet_address.lower(), t.market_id)

                if key not in self._fill_buffer:
                    self._fill_buffer[key] = {
                        "total_usd": 0,
                        "count": 0,
                        "side": t.side,
                        "outcome": t.outcome,
                        "first_ts": now,
                        "last_ts": now,
                        "emitted": False,
                        "trade_ref": t,  # Referencia al último trade para metadatos
                    }

                buf = self._fill_buffer[key]
                buf["total_usd"] += t.size
                buf["count"] += 1
                buf["last_ts"] = now
                buf["trade_ref"] = t
                self.stats["fills_aggregated"] += 1

                # Detectar si superó el umbral y no se emitió aún
                if buf["total_usd"] >= self.min_whale_size and not buf["emitted"]:
                    buf["emitted"] = True
                    # Crear whale trade sintético con el total agregado
                    agg_hash = hashlib.md5(
                        f"agg_{key[0]}_{key[1]}_{buf['first_ts']:.0f}".encode()
                    ).hexdigest()
                    whale = Trade(
                        transaction_hash=f"agg_{agg_hash}",
                        market_id=t.market_id,
                        market_question=t.market_question,
                        market_slug=t.market_slug,
                        wallet_address=t.wallet_address,
                        side=buf["side"],
                        size=round(buf["total_usd"], 2),
                        price=t.price,
                        timestamp=t.timestamp,
                        outcome=buf["outcome"],
                        market_end_date=t.market_end_date,
                        market_category=t.market_category,
                        trader_name=t.trader_name,
                        trader_pseudonym=t.trader_pseudonym,
                        trader_profile_image=t.trader_profile_image,
                    )
                    detected.append(whale)
                    self.stats["whales_from_fills"] += 1
                    print(
                        f"[WhaleScanner] Agregación detectada: "
                        f"{t.wallet_address[:10]}... ${buf['total_usd']:,.0f} "
                        f"({buf['count']} fills) en {t.market_question[:40]}",
                        flush=True,
                    )

        return detected

    # ── Parte 2: Monitoreo de actividad de wallets conocidas ──────────

    async def _refresh_known_wallets(self):
        """Refrescar lista de wallets a monitorear desde DB."""
        now = time.time()
        if now - self._last_wallet_refresh < self._wallet_refresh_interval:
            return

        self._last_wallet_refresh = now
        wallets = set()

        try:
            # 1. Wallets del ranking de whales (top por volumen)
            ranking = await self.db.get_whale_ranking(
                min_size=self.min_whale_size, min_trades=2, sort_by="volume"
            )
            for w in (ranking or [])[:50]:
                wallets.add(w["address"].lower())

            # 2. Wallets watchlisted
            try:
                watchlisted = await self.db.get_watchlisted_wallets()
                if isinstance(watchlisted, set):
                    wallets.update(watchlisted)
                else:
                    for w in (watchlisted or []):
                        addr = w.get("address", w.get("wallet_address", "")).lower() if isinstance(w, dict) else str(w).lower()
                        if addr:
                            wallets.add(addr)
            except Exception:
                pass

            # 3. Wallets activas recientes (las que tienen whale trades en últimos 7 días)
            try:
                async with self.db._pool.acquire() as conn:
                    rows = await conn.fetch("""
                        SELECT DISTINCT wallet_address
                        FROM whale_trades
                        WHERE created_at >= NOW() - INTERVAL '7 days'
                          AND size >= $1
                    """, self.min_whale_size)
                    for r in rows:
                        wallets.add(r["wallet_address"].lower())
            except Exception:
                pass

            self._known_wallets = list(wallets)
            self.stats["wallets_monitored"] = len(self._known_wallets)
            if self._known_wallets:
                print(
                    f"[WhaleScanner] Monitoreando {len(self._known_wallets)} wallets conocidas",
                    flush=True,
                )
        except Exception as e:
            print(f"[WhaleScanner] Error refrescando wallets: {e}", flush=True)

    async def scan_wallet_activity(self) -> list[Trade]:
        """Escanear actividad reciente de wallets conocidas.

        Consulta /activity de cada wallet y detecta trades grandes.
        Retorna lista de whale trades encontrados.
        """
        await self._refresh_known_wallets()

        if not self._known_wallets:
            return []

        detected = []
        now = time.time()
        self.stats["last_scan_time"] = datetime.now(timezone.utc).isoformat()

        async with httpx.AsyncClient(timeout=15) as client:
            # Procesar en batches para no saturar la API
            batch_size = 10
            for i in range(0, len(self._known_wallets), batch_size):
                batch = self._known_wallets[i:i + batch_size]
                tasks = [
                    self._scan_single_wallet(client, addr)
                    for addr in batch
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for addr, result in zip(batch, results):
                    if isinstance(result, Exception):
                        continue
                    if result:
                        detected.extend(result)

                # Pausa entre batches para rate limiting
                if i + batch_size < len(self._known_wallets):
                    await asyncio.sleep(1)

        if detected:
            print(
                f"[WhaleScanner] Activity scan: {len(detected)} whale trades de "
                f"{len(self._known_wallets)} wallets",
                flush=True,
            )

        return detected

    async def _scan_single_wallet(
        self, client: httpx.AsyncClient, address: str
    ) -> list[Trade]:
        """Escanear actividad reciente de una wallet."""
        try:
            r = await client.get(
                f"{_POLY_DATA_API}/activity",
                params={"user": address, "limit": 20, "offset": 0},
            )
            if r.status_code != 200:
                return []

            activities = r.json()
            if not isinstance(activities, list):
                return []

            detected = []
            last_seen_ts = self._wallet_last_seen.get(address, 0)
            max_ts = last_seen_ts

            for a in activities:
                # Parsear timestamp
                ts_val = a.get("timestamp", 0)
                if isinstance(ts_val, str):
                    try:
                        ts_val = datetime.fromisoformat(
                            ts_val.replace("Z", "+00:00")
                        ).timestamp()
                    except Exception:
                        continue
                ts_val = float(ts_val)

                # Solo procesar trades nuevos
                if ts_val <= last_seen_ts:
                    continue
                max_ts = max(max_ts, ts_val)

                # Calcular USD value
                usdc_size = float(a.get("usdcSize", 0))
                if usdc_size <= 0:
                    size_raw = float(a.get("size", 0))
                    price = float(a.get("price", 0))
                    usdc_size = size_raw * price

                # Solo whale trades
                if usdc_size < self.min_whale_size:
                    continue

                # Crear Trade object
                tx_hash = a.get("transactionHash", "")
                if not tx_hash:
                    tx_hash = hashlib.md5(
                        f"act_{address}_{ts_val}_{a.get('conditionId','')}".encode()
                    ).hexdigest()
                    tx_hash = f"act_{tx_hash}"

                try:
                    ts_dt = datetime.fromtimestamp(ts_val, tz=timezone.utc)
                except Exception:
                    ts_dt = datetime.now(timezone.utc)

                trade = Trade(
                    transaction_hash=tx_hash,
                    market_id=a.get("conditionId", ""),
                    market_question=a.get("title", ""),
                    market_slug=a.get("eventSlug", a.get("slug", "")),
                    wallet_address=address,
                    side=a.get("side", "BUY"),
                    size=round(usdc_size, 2),
                    price=float(a.get("price", 0)),
                    timestamp=ts_dt,
                    outcome=a.get("outcome", ""),
                    trader_name=a.get("name") or None,
                    trader_pseudonym=a.get("pseudonym") or None,
                    trader_profile_image=a.get("profileImage") or a.get("profileImageOptimized") or None,
                )
                detected.append(trade)
                self.stats["whales_from_activity"] += 1

            # Actualizar last seen
            if max_ts > last_seen_ts:
                self._wallet_last_seen[address] = max_ts

            return detected

        except Exception as e:
            return []

    # ── Loop principal de actividad ───────────────────────────────────

    async def run_activity_loop(self):
        """Loop infinito que escanea actividad de wallets conocidas cada N minutos."""
        print(f"[WhaleScanner] Activity loop iniciado (cada {self.activity_scan_interval}s)", flush=True)
        # Esperar un poco para que la DB tenga datos
        await asyncio.sleep(30)

        while True:
            try:
                whale_trades = await self.scan_wallet_activity()

                # Guardar whale trades detectados
                if whale_trades:
                    saved = 0
                    for wt in whale_trades:
                        if await self.db.save_whale_trade(wt):
                            saved += 1
                    if saved:
                        print(
                            f"[WhaleScanner] Activity: {saved} nuevos whale trades guardados",
                            flush=True,
                        )

            except Exception as e:
                print(f"[WhaleScanner] Error en activity loop: {e}", flush=True)

            await asyncio.sleep(self.activity_scan_interval)
