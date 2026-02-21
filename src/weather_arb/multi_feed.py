"""Fuentes adicionales de pronóstico meteorológico.

Complementa Open-Meteo Ensemble con:
1. Weather.gov (NOAA) — ciudades US, gratis sin API key
2. WeatherAPI.com — global, gratis con API key (1M calls/mes)
3. Visual Crossing — global, gratis con API key (1000 calls/día)

Genera un "consensus forecast" combinando múltiples fuentes.
"""
import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog
import httpx

from src import config
from src.weather_arb.weather_feed import WEATHER_CITIES, CITY_BY_SLUG

logger = structlog.get_logger()


@dataclass
class SourceForecast:
    """Pronóstico de una fuente específica."""
    source: str               # "open_meteo", "weather_gov", "weatherapi", "visual_crossing"
    city: str                 # Slug de la ciudad
    date: str                 # Fecha ISO
    max_temp: Optional[float] = None  # Temperatura máxima pronosticada
    min_temp: Optional[float] = None
    unit: str = "fahrenheit"
    fetched_at: float = 0.0
    error: str = ""


@dataclass
class ConsensusForecast:
    """Pronóstico consenso de múltiples fuentes."""
    city: str
    date: str
    unit: str
    sources: list[SourceForecast] = field(default_factory=list)
    consensus_max: Optional[float] = None  # Media de todas las fuentes
    spread: float = 0.0                     # Diferencia máxima entre fuentes
    agreement_count: int = 0               # Fuentes que coinciden en ±1°
    confidence_boost: float = 0.0          # Boost de confianza (0-0.3)

    @property
    def n_sources(self) -> int:
        return len([s for s in self.sources if s.max_temp is not None])

    def to_dict(self) -> dict:
        return {
            "city": self.city,
            "date": self.date,
            "unit": self.unit,
            "consensus_max": round(self.consensus_max, 1) if self.consensus_max else None,
            "spread": round(self.spread, 1),
            "agreement_count": self.agreement_count,
            "confidence_boost": round(self.confidence_boost, 2),
            "n_sources": self.n_sources,
            "sources": [
                {
                    "source": s.source,
                    "max_temp": round(s.max_temp, 1) if s.max_temp is not None else None,
                    "error": s.error,
                }
                for s in self.sources
            ],
        }


class MultiWeatherFeed:
    """Combina múltiples fuentes de pronóstico meteorológico.

    Para cada ciudad/fecha, obtiene pronósticos de hasta 3 fuentes adicionales
    y genera un consensus forecast con mayor confianza.
    """

    def __init__(self):
        self._consensus: dict[str, dict[str, ConsensusForecast]] = {}  # city → {date → consensus}
        self._last_refresh = 0.0
        self._refresh_interval = 3600  # 1 hora (las fuentes adicionales son más lentas)
        self._running = False
        self._weatherapi_key = ""
        self._visual_crossing_key = ""
        self._enabled = False

    def configure(self, cfg: dict):
        """Configurar desde config o DB."""
        self._weatherapi_key = cfg.get("weatherapi_key", "")
        self._visual_crossing_key = cfg.get("visual_crossing_key", "")
        self._enabled = cfg.get("multi_source_enabled", False)
        self._refresh_interval = cfg.get("multi_source_refresh_sec", 3600)

    async def start(self):
        """Loop principal: actualizar fuentes adicionales periódicamente."""
        self._running = True
        print(f"[MultiFeed] Iniciando multi-source feed (enabled={self._enabled})", flush=True)
        while self._running:
            try:
                if self._enabled:
                    now = time.time()
                    if now - self._last_refresh >= self._refresh_interval or self._last_refresh == 0:
                        await self._refresh_all()
                        self._last_refresh = now
            except Exception as e:
                print(f"[MultiFeed] Error: {e}", flush=True)
            await asyncio.sleep(120)

    async def stop(self):
        self._running = False

    def get_consensus(self, city_slug: str, date: str) -> Optional[ConsensusForecast]:
        """Obtener consensus forecast para ciudad/fecha."""
        return self._consensus.get(city_slug, {}).get(date)

    def get_all_consensus(self) -> dict:
        """Todos los consensus para dashboard."""
        result = {}
        for city_slug, dates in self._consensus.items():
            result[city_slug] = {d: c.to_dict() for d, c in dates.items()}
        return result

    async def _refresh_all(self):
        """Actualizar todas las fuentes para todas las ciudades."""
        now_utc = datetime.now(timezone.utc)
        dates = [(now_utc + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(3)]
        updated = 0

        async with httpx.AsyncClient(timeout=15) as client:
            for city in WEATHER_CITIES:
                slug = city["slug"]
                if slug not in self._consensus:
                    self._consensus[slug] = {}

                for date_str in dates:
                    sources = []

                    # 1. Weather.gov (solo US)
                    if city.get("country") == "us":
                        src = await self._fetch_weather_gov(client, city, date_str)
                        if src:
                            sources.append(src)

                    # 2. WeatherAPI.com
                    if self._weatherapi_key:
                        src = await self._fetch_weatherapi(client, city, date_str)
                        if src:
                            sources.append(src)

                    # 3. Visual Crossing
                    if self._visual_crossing_key:
                        src = await self._fetch_visual_crossing(client, city, date_str)
                        if src:
                            sources.append(src)

                    if sources:
                        consensus = self._compute_consensus(slug, date_str, city["unit"], sources)
                        self._consensus[slug][date_str] = consensus
                        updated += 1

                await asyncio.sleep(0.5)

        active_sources = []
        if any(c.get("country") == "us" for c in WEATHER_CITIES):
            active_sources.append("Weather.gov")
        if self._weatherapi_key:
            active_sources.append("WeatherAPI")
        if self._visual_crossing_key:
            active_sources.append("VisualCrossing")

        print(f"[MultiFeed] Refresh: {updated} forecasts, fuentes=[{', '.join(active_sources)}]", flush=True)

    async def _fetch_weather_gov(self, client: httpx.AsyncClient,
                                  city: dict, date_str: str) -> Optional[SourceForecast]:
        """Obtener pronóstico de Weather.gov (NOAA) — solo ciudades US."""
        try:
            lat = city["lat"]
            lon = city["lon"]
            # Paso 1: obtener URL del forecast
            resp = await client.get(
                f"https://api.weather.gov/points/{lat},{lon}",
                headers={"User-Agent": "PolymarketWeatherBot/1.0 (contact@example.com)"},
            )
            if resp.status_code != 200:
                return SourceForecast(source="weather_gov", city=city["slug"],
                                      date=date_str, error=f"points API {resp.status_code}")

            data = resp.json()
            forecast_url = data.get("properties", {}).get("forecast", "")
            if not forecast_url:
                return SourceForecast(source="weather_gov", city=city["slug"],
                                      date=date_str, error="no forecast URL")

            # Paso 2: obtener forecast
            resp2 = await client.get(
                forecast_url,
                headers={"User-Agent": "PolymarketWeatherBot/1.0 (contact@example.com)"},
            )
            if resp2.status_code != 200:
                return SourceForecast(source="weather_gov", city=city["slug"],
                                      date=date_str, error=f"forecast API {resp2.status_code}")

            periods = resp2.json().get("properties", {}).get("periods", [])
            # Buscar el período diurno que coincida con la fecha
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
            for p in periods:
                if not p.get("isDaytime"):
                    continue
                try:
                    start = datetime.fromisoformat(p["startTime"].replace("Z", "+00:00"))
                    if start.date() == target:
                        temp = p.get("temperature")
                        if temp is not None:
                            return SourceForecast(
                                source="weather_gov",
                                city=city["slug"],
                                date=date_str,
                                max_temp=float(temp),
                                unit=city["unit"],
                                fetched_at=time.time(),
                            )
                except (ValueError, KeyError):
                    continue

            return SourceForecast(source="weather_gov", city=city["slug"],
                                  date=date_str, error="fecha no encontrada")
        except Exception as e:
            return SourceForecast(source="weather_gov", city=city["slug"],
                                  date=date_str, error=str(e)[:100])

    async def _fetch_weatherapi(self, client: httpx.AsyncClient,
                                 city: dict, date_str: str) -> Optional[SourceForecast]:
        """Obtener pronóstico de WeatherAPI.com — global."""
        try:
            lat = city["lat"]
            lon = city["lon"]
            resp = await client.get(
                "https://api.weatherapi.com/v1/forecast.json",
                params={
                    "key": self._weatherapi_key,
                    "q": f"{lat},{lon}",
                    "days": 3,
                    "aqi": "no",
                    "alerts": "no",
                },
            )
            if resp.status_code != 200:
                return SourceForecast(source="weatherapi", city=city["slug"],
                                      date=date_str, error=f"API {resp.status_code}")

            data = resp.json()
            for day in data.get("forecast", {}).get("forecastday", []):
                if day.get("date") == date_str:
                    day_data = day.get("day", {})
                    if city["unit"] == "fahrenheit":
                        max_temp = day_data.get("maxtemp_f")
                    else:
                        max_temp = day_data.get("maxtemp_c")

                    if max_temp is not None:
                        return SourceForecast(
                            source="weatherapi",
                            city=city["slug"],
                            date=date_str,
                            max_temp=float(max_temp),
                            unit=city["unit"],
                            fetched_at=time.time(),
                        )

            return SourceForecast(source="weatherapi", city=city["slug"],
                                  date=date_str, error="fecha no encontrada")
        except Exception as e:
            return SourceForecast(source="weatherapi", city=city["slug"],
                                  date=date_str, error=str(e)[:100])

    async def _fetch_visual_crossing(self, client: httpx.AsyncClient,
                                      city: dict, date_str: str) -> Optional[SourceForecast]:
        """Obtener pronóstico de Visual Crossing — global."""
        try:
            lat = city["lat"]
            lon = city["lon"]
            unit_group = "us" if city["unit"] == "fahrenheit" else "metric"
            resp = await client.get(
                f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
                f"/{lat},{lon}/{date_str}",
                params={
                    "unitGroup": unit_group,
                    "key": self._visual_crossing_key,
                    "include": "days",
                    "contentType": "json",
                },
            )
            if resp.status_code != 200:
                return SourceForecast(source="visual_crossing", city=city["slug"],
                                      date=date_str, error=f"API {resp.status_code}")

            data = resp.json()
            days = data.get("days", [])
            if days:
                max_temp = days[0].get("tempmax")
                if max_temp is not None:
                    return SourceForecast(
                        source="visual_crossing",
                        city=city["slug"],
                        date=date_str,
                        max_temp=float(max_temp),
                        unit=city["unit"],
                        fetched_at=time.time(),
                    )

            return SourceForecast(source="visual_crossing", city=city["slug"],
                                  date=date_str, error="sin datos")
        except Exception as e:
            return SourceForecast(source="visual_crossing", city=city["slug"],
                                  date=date_str, error=str(e)[:100])

    def _compute_consensus(self, city_slug: str, date_str: str,
                           unit: str, sources: list[SourceForecast]) -> ConsensusForecast:
        """Calcular consensus forecast a partir de múltiples fuentes."""
        valid = [s for s in sources if s.max_temp is not None]
        consensus = ConsensusForecast(city=city_slug, date=date_str, unit=unit, sources=sources)

        if not valid:
            return consensus

        temps = [s.max_temp for s in valid]
        consensus.consensus_max = sum(temps) / len(temps)
        consensus.spread = max(temps) - min(temps) if len(temps) > 1 else 0

        # Contar fuentes que coinciden en ±1° con la media
        if consensus.consensus_max is not None:
            consensus.agreement_count = sum(
                1 for t in temps if abs(t - consensus.consensus_max) <= 1.0
            )

        # Confidence boost basado en acuerdo
        n = len(valid)
        if n >= 3 and consensus.spread <= 1.0:
            consensus.confidence_boost = 0.25  # Excelente acuerdo
        elif n >= 2 and consensus.spread <= 2.0:
            consensus.confidence_boost = 0.15  # Buen acuerdo
        elif n >= 2 and consensus.spread <= 3.0:
            consensus.confidence_boost = 0.05  # Acuerdo moderado
        else:
            consensus.confidence_boost = 0.0   # Divergencia — no boost

        return consensus

    def get_status(self) -> dict:
        """Estado del multi-feed para dashboard."""
        total = sum(len(dates) for dates in self._consensus.values())
        sources_active = []
        # Siempre intentamos Weather.gov para US
        sources_active.append("weather_gov")
        if self._weatherapi_key:
            sources_active.append("weatherapi")
        if self._visual_crossing_key:
            sources_active.append("visual_crossing")

        return {
            "enabled": self._enabled,
            "sources_active": sources_active,
            "total_consensus": total,
            "last_refresh": self._last_refresh,
            "refresh_interval": self._refresh_interval,
        }
