"""Early Weather Detector — Pre-market analysis para velocidad.

Prepara análisis ANTES de que abra el mercado en Polymarket.
Calcula forecasts anticipados y ejecuta trades inmediatamente
cuando se detecta un mercado nuevo.

Equivalente a crypto_arb/early_detector.py pero para weather.

Flujo:
1. Pre-genera slugs esperados para los próximos 3 días
2. Mantiene forecasts precalculados en memoria
3. Escanea Polymarket cada 60s buscando mercados nuevos
4. Cuando detecta mercado nuevo, compara forecast precalculado vs odds
5. Si hay edge, genera señal inmediata (ejecutar en <15s)
"""
import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog
import httpx

from src import config
from src.weather_arb.weather_feed import WeatherFeed, WEATHER_CITIES, CITY_BY_SLUG

logger = structlog.get_logger()

GAMMA_API = "https://gamma-api.polymarket.com"

MONTH_NAMES = {
    1: "january", 2: "february", 3: "march", 4: "april",
    5: "may", 6: "june", 7: "july", 8: "august",
    9: "september", 10: "october", 11: "november", 12: "december",
}


@dataclass
class EarlyWeatherSignal:
    """Señal de early entry para weather."""
    city: str
    city_name: str
    date: str
    range_label: str
    ensemble_prob: float
    poly_odds: float
    edge_pct: float
    confidence: float
    condition_id: str
    market_question: str
    event_slug: str
    token_id: str = ""
    strategy: str = "early_entry"
    time_since_open_sec: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EarlyWeatherDetector:
    """Detecta mercados weather nuevos y genera señales de entrada rápida."""

    def __init__(self, weather_feed: WeatherFeed):
        self.feed = weather_feed
        self._known_slugs: set[str] = set()  # Slugs ya detectados
        self._new_market_times: dict[str, float] = {}  # slug → timestamp primera detección
        self._signals: list[EarlyWeatherSignal] = []
        self._running = False
        self._enabled = False
        self._scan_interval = 60  # Escanear cada 60s (más agresivo que detector normal)
        self._min_edge = 5.0  # Edge mínimo para early entry (más bajo que normal)
        self._min_confidence = 40.0
        self._max_poly_odds = 0.90
        self._entry_window_sec = 300  # Solo señales en primeros 5 min del mercado
        self._cities: Optional[list[str]] = None

    def configure(self, cfg: dict):
        """Actualizar configuración."""
        self._enabled = cfg.get("early_enabled", self._enabled)
        self._scan_interval = cfg.get("early_scan_interval", self._scan_interval)
        self._min_edge = cfg.get("early_min_edge", self._min_edge)
        self._min_confidence = cfg.get("early_min_confidence", self._min_confidence)
        self._max_poly_odds = cfg.get("early_max_poly_odds", self._max_poly_odds)
        self._entry_window_sec = cfg.get("early_entry_window_sec", self._entry_window_sec)
        self._cities = cfg.get("enabled_cities", self._cities)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self):
        """Loop principal: escanear mercados nuevos."""
        self._running = True
        if not self._enabled:
            print("[EarlyWeather] Desactivado", flush=True)
            return

        print(f"[EarlyWeather] Iniciando early detector, scan cada {self._scan_interval}s", flush=True)

        while self._running:
            try:
                new_signals = await self._scan_for_new_markets()
                for sig in new_signals:
                    self._signals.append(sig)
                    print(f"[EarlyWeather] ⚡ EARLY SIGNAL: {sig.city_name} {sig.date} "
                          f"→ {sig.range_label} edge={sig.edge_pct:.1f}% "
                          f"({sig.time_since_open_sec:.0f}s desde apertura)", flush=True)
            except Exception as e:
                print(f"[EarlyWeather] Error: {e}", flush=True)

            await asyncio.sleep(self._scan_interval)

    async def stop(self):
        self._running = False

    def _get_expected_slugs(self) -> list[dict]:
        """Generar slugs esperados para mercados de los próximos 3 días."""
        now_utc = datetime.now(timezone.utc)
        expected = []
        cities = self._get_cities()

        for city in cities:
            for day_offset in range(4):  # Hoy + 3 días
                target = now_utc + timedelta(days=day_offset)
                month_name = MONTH_NAMES[target.month]
                day = target.day
                year = target.year
                slug = f"highest-temperature-in-{city['slug']}-on-{month_name}-{day}-{year}"
                expected.append({
                    "slug": slug,
                    "city": city,
                    "date": target.strftime("%Y-%m-%d"),
                })

        return expected

    def _get_cities(self) -> list[dict]:
        if self._cities:
            return [c for c in WEATHER_CITIES if c["slug"] in self._cities]
        return list(WEATHER_CITIES)

    async def _scan_for_new_markets(self) -> list[EarlyWeatherSignal]:
        """Buscar mercados nuevos en Polymarket y generar señales rápidas."""
        signals = []
        expected = self._get_expected_slugs()
        now = time.time()

        async with httpx.AsyncClient(timeout=10) as client:
            for item in expected:
                slug = item["slug"]

                # Si ya lo conocemos y pasó la ventana de entry, skip
                if slug in self._known_slugs:
                    first_seen = self._new_market_times.get(slug, 0)
                    if now - first_seen > self._entry_window_sec:
                        continue

                try:
                    resp = await client.get(
                        f"{GAMMA_API}/events",
                        params={"slug": slug},
                    )
                    if resp.status_code != 200 or not resp.json():
                        await asyncio.sleep(0.3)
                        continue

                    events = resp.json()
                    ev = events[0]
                    if ev.get("closed") or not ev.get("active"):
                        self._known_slugs.add(slug)
                        await asyncio.sleep(0.2)
                        continue

                    # ¡Mercado encontrado!
                    is_new = slug not in self._known_slugs
                    if is_new:
                        self._known_slugs.add(slug)
                        self._new_market_times[slug] = now
                        print(f"[EarlyWeather] 🆕 Mercado nuevo detectado: {slug}", flush=True)

                    # Verificar si estamos dentro de la ventana de early entry
                    first_seen = self._new_market_times.get(slug, now)
                    time_since_open = now - first_seen
                    if time_since_open > self._entry_window_sec:
                        continue

                    # Generar señales para los rangos de este mercado
                    city = item["city"]
                    date_str = item["date"]
                    market_signals = await self._evaluate_market(
                        ev, city, date_str, slug, time_since_open
                    )
                    signals.extend(market_signals)

                except Exception:
                    pass

                await asyncio.sleep(0.2)

        return signals

    async def _evaluate_market(self, event: dict, city: dict, date_str: str,
                                slug: str, time_since_open: float) -> list[EarlyWeatherSignal]:
        """Evaluar un mercado recién encontrado contra forecasts precalculados."""
        signals = []
        city_slug = city["slug"]

        # Obtener forecast precalculado
        forecast = self.feed.get_forecast(city_slug, date_str)
        if not forecast or not forecast.ensemble_max_temps:
            return signals

        import json
        markets = event.get("markets", [])

        for m in markets:
            if m.get("closed"):
                continue
            cid = m.get("conditionId", "")
            if not cid:
                continue

            group_title = m.get("groupItemTitle", "")
            if not group_title:
                continue

            # Parsear rango
            from src.weather_arb.detector import WeatherArbDetector
            range_info = WeatherArbDetector._parse_range(None, group_title, city["unit"])
            if not range_info:
                continue

            # Obtener odds
            outcome_prices = m.get("outcomePrices", "[]")
            try:
                prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                yes_price = float(prices[0]) if prices else 0
            except (json.JSONDecodeError, IndexError, ValueError):
                yes_price = 0

            if yes_price <= 0 or yes_price >= 1 or yes_price > self._max_poly_odds:
                continue

            # Token ID
            clob_token_ids = m.get("clobTokenIds", "[]")
            try:
                token_ids = json.loads(clob_token_ids) if isinstance(clob_token_ids, str) else clob_token_ids
                yes_token = token_ids[0] if token_ids else ""
            except (json.JSONDecodeError, IndexError):
                yes_token = ""

            # Calcular probabilidad ensemble para este rango
            range_defs = [{"label": group_title, "low": range_info["low"], "high": range_info["high"]}]
            probs = self.feed.compute_range_probabilities(forecast, range_defs)
            ensemble_prob = probs.get(group_title, 0)

            if ensemble_prob < 0.05:
                continue

            edge_pct = (ensemble_prob - yes_price) * 100
            confidence = forecast.confidence * 100

            if edge_pct < self._min_edge:
                continue
            if confidence < self._min_confidence:
                continue

            # ¡Señal early entry!
            signal = EarlyWeatherSignal(
                city=city_slug,
                city_name=city["name"],
                date=date_str,
                range_label=group_title,
                ensemble_prob=round(ensemble_prob, 4),
                poly_odds=round(yes_price, 4),
                edge_pct=round(edge_pct, 2),
                confidence=round(confidence, 1),
                condition_id=cid,
                market_question=m.get("question", ""),
                event_slug=slug,
                token_id=yes_token,
                time_since_open_sec=round(time_since_open, 1),
            )
            signals.append(signal)

        return signals

    def get_recent_signals(self, limit: int = 50) -> list[dict]:
        """Señales recientes como dicts para API."""
        # Solo señales de las últimas 24 horas
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = [s for s in self._signals if s.timestamp > cutoff]
        recent.sort(key=lambda s: s.edge_pct, reverse=True)
        return [
            {
                "city": s.city,
                "city_name": s.city_name,
                "date": s.date,
                "range_label": s.range_label,
                "ensemble_prob": s.ensemble_prob,
                "poly_odds": s.poly_odds,
                "edge_pct": s.edge_pct,
                "confidence": s.confidence,
                "condition_id": s.condition_id,
                "market_question": s.market_question,
                "event_slug": s.event_slug,
                "token_id": s.token_id,
                "strategy": s.strategy,
                "time_since_open_sec": s.time_since_open_sec,
                "timestamp": s.timestamp.isoformat(),
            }
            for s in recent[:limit]
        ]

    def get_stats(self) -> dict:
        """Estadísticas del early detector."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = [s for s in self._signals if s.timestamp > cutoff]
        return {
            "enabled": self._enabled,
            "known_markets": len(self._known_slugs),
            "signals_24h": len(recent),
            "scan_interval": self._scan_interval,
            "entry_window_sec": self._entry_window_sec,
        }
