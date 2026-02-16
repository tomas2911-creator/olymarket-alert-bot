"""Binary Complement Arbitrage Scanner — v10.0

Escanea mercados de Polymarket buscando oportunidades donde
YES_ask + NO_ask < $1.00, lo que garantiza profit sin riesgo.

Según investigación, bots han generado >$39.5M con esta estrategia desde 2024.
"""
import asyncio
import time
from datetime import datetime, timezone
from typing import Optional
import structlog

logger = structlog.get_logger()

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"


class ComplementArbScanner:
    """Escanea mercados buscando YES+NO < $1 para arbitrage sin riesgo."""

    def __init__(self, db):
        self.db = db
        self._enabled = False
        self._min_edge_pct = 1.0  # Edge mínimo en % (después de fees)
        self._max_markets = 100
        self._scan_interval = 60  # segundos entre scans
        self._last_scan = 0
        self._opportunities: list[dict] = []
        self._daily_found = 0
        self._daily_date = ""

    async def initialize(self):
        """Cargar config desde DB."""
        try:
            raw = await self.db.get_config_bulk([
                "arb_complement_enabled", "arb_complement_min_edge",
                "arb_complement_scan_interval", "arb_complement_max_markets",
            ])
            self._enabled = raw.get("arb_complement_enabled") == "true"
            self._min_edge_pct = float(raw.get("arb_complement_min_edge", 1.0))
            self._scan_interval = int(raw.get("arb_complement_scan_interval", 60))
            self._max_markets = int(raw.get("arb_complement_max_markets", 100))
            status = "ACTIVADO" if self._enabled else "DESACTIVADO"
            print(f"[ComplementArb] {status} | min_edge={self._min_edge_pct}% interval={self._scan_interval}s",
                  flush=True)
        except Exception as e:
            print(f"[ComplementArb] Error inicializando: {e}", flush=True)

    async def scan(self) -> list[dict]:
        """Escanear mercados activos buscando YES+NO < $1.
        Retorna lista de oportunidades encontradas.
        """
        if not self._enabled:
            return []

        now = time.time()
        if now - self._last_scan < self._scan_interval:
            return self._opportunities

        self._last_scan = now
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._daily_date != today:
            self._daily_found = 0
            self._daily_date = today

        opportunities = []

        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                # Obtener mercados activos con liquidez
                resp = await client.get(f"{GAMMA_HOST}/markets", params={
                    "active": "true",
                    "closed": "false",
                    "limit": self._max_markets,
                    "order": "liquidityNum",
                    "ascending": "false",
                })
                if resp.status_code != 200:
                    return self._opportunities

                markets = resp.json()
                if not isinstance(markets, list):
                    return self._opportunities

                checked = 0
                for market in markets:
                    cid = market.get("conditionId", market.get("condition_id", ""))
                    if not cid:
                        continue

                    try:
                        # Obtener orderbook del CLOB
                        clob_resp = await client.get(f"{CLOB_HOST}/markets/{cid}")
                        if clob_resp.status_code != 200:
                            continue
                        data = clob_resp.json()
                        tokens = data.get("tokens", [])
                        if len(tokens) < 2:
                            continue

                        # Obtener best ask de YES y NO
                        yes_price = None
                        no_price = None
                        for token in tokens:
                            outcome = (token.get("outcome", "") or "").lower()
                            price = float(token.get("price", 0))
                            if outcome == "yes":
                                yes_price = price
                            elif outcome == "no":
                                no_price = price

                        if yes_price is None or no_price is None:
                            continue
                        if yes_price <= 0 or no_price <= 0:
                            continue

                        total = yes_price + no_price
                        if total >= 1.0:
                            continue

                        # Encontrado: YES + NO < $1.00
                        edge = (1.0 - total) / total * 100  # % profit
                        if edge < self._min_edge_pct:
                            continue

                        question = market.get("question", "")[:80]
                        slug = market.get("slug", "")
                        liquidity = float(market.get("liquidityNum", 0))

                        opp = {
                            "condition_id": cid,
                            "question": question,
                            "slug": slug,
                            "yes_price": round(yes_price, 4),
                            "no_price": round(no_price, 4),
                            "total": round(total, 4),
                            "edge_pct": round(edge, 2),
                            "profit_per_100": round((1.0 - total) * 100, 2),
                            "liquidity": round(liquidity, 0),
                            "found_at": datetime.now(timezone.utc).isoformat(),
                        }
                        opportunities.append(opp)
                        self._daily_found += 1

                        print(f"[ComplementArb] 💰 ARB: {question[:50]} "
                              f"YES={yes_price:.3f} + NO={no_price:.3f} = {total:.3f} "
                              f"(edge={edge:.1f}%, profit=${opp['profit_per_100']:.2f}/100)",
                              flush=True)

                        checked += 1
                        # Rate limit
                        if checked % 10 == 0:
                            await asyncio.sleep(0.5)

                    except Exception as e:
                        continue

                if opportunities:
                    print(f"[ComplementArb] Scan completo: {len(opportunities)} oportunidades "
                          f"de {len(markets)} mercados", flush=True)

        except Exception as e:
            print(f"[ComplementArb] Error en scan: {e}", flush=True)

        self._opportunities = opportunities
        return opportunities

    def get_status(self) -> dict:
        """Estado actual para el dashboard."""
        return {
            "enabled": self._enabled,
            "opportunities": len(self._opportunities),
            "daily_found": self._daily_found,
            "min_edge_pct": self._min_edge_pct,
            "last_scan": self._last_scan,
            "top_opportunities": sorted(
                self._opportunities, key=lambda x: x["edge_pct"], reverse=True
            )[:10],
        }
