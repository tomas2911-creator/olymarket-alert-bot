"""Feed de observaciones METAR en tiempo real desde aviationweather.gov.

Consulta datos METAR (observaciones reales de aeropuertos) cada 5 minutos
para las estaciones que Polymarket usa como fuente de resolución.

Esto permite saber la temperatura ACTUAL y el máximo observado del día,
dando edge cuando la temperatura ya alcanzó su pico pero el mercado
no ajustó los precios.

API: https://aviationweather.gov/api/data/metar — gratuita, sin API key.
"""
import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
import structlog
import httpx

from src.weather_arb.weather_feed import WEATHER_CITIES

logger = structlog.get_logger()

METAR_API_URL = "https://aviationweather.gov/api/data/metar"


@dataclass
class StationObservation:
    """Observación METAR de una estación de aeropuerto."""
    station: str           # ICAO code (ej: "KLGA")
    city_slug: str         # Slug de la ciudad (ej: "nyc")
    temp_c: Optional[float] = None   # Temperatura actual °C
    temp_f: Optional[float] = None   # Temperatura actual °F
    dewpoint_c: Optional[float] = None
    wind_speed_kt: Optional[float] = None
    observation_time: Optional[datetime] = None
    raw_metar: str = ""
    updated_at: float = 0.0
    # Tracking del máximo del día
    observed_high_c: float = -999.0
    observed_high_f: float = -999.0
    observations_today: int = 0
    high_reached_at: Optional[datetime] = None  # Hora en que se alcanzó el máximo


class MetarFeed:
    """Mantiene observaciones METAR actualizadas para todas las estaciones.

    Consulta aviationweather.gov cada 5 min para obtener la temperatura
    actual de cada estación ICAO que usa Polymarket para resolución.
    """

    def __init__(self, cities: Optional[list[str]] = None,
                 refresh_interval: int = 300):
        """
        Args:
            cities: Lista de slugs de ciudades a monitorear. None = todas.
            refresh_interval: Segundos entre refreshes (default 5 min).
        """
        if cities:
            self._cities = [c for c in WEATHER_CITIES if c["slug"] in cities]
        else:
            self._cities = list(WEATHER_CITIES)
        self._refresh_interval = refresh_interval
        self._observations: dict[str, StationObservation] = {}  # city_slug → obs
        self._last_refresh = 0.0
        self._running = False
        self._errors_in_a_row = 0
        self._today_str = ""  # Para resetear highs al cambiar de día
        self._enabled = True

    @property
    def is_running(self) -> bool:
        return self._running

    def get_observation(self, city_slug: str) -> Optional[StationObservation]:
        """Obtener observación actual para una ciudad."""
        return self._observations.get(city_slug)

    def get_observed_high(self, city_slug: str, unit: str = "fahrenheit") -> Optional[float]:
        """Obtener el máximo observado del día para una ciudad.

        Args:
            city_slug: Slug de la ciudad
            unit: "fahrenheit" o "celsius"

        Returns:
            Máximo observado o None si no hay datos
        """
        obs = self._observations.get(city_slug)
        if not obs:
            return None
        if unit == "fahrenheit":
            return obs.observed_high_f if obs.observed_high_f > -999 else None
        return obs.observed_high_c if obs.observed_high_c > -999 else None

    def is_observation_fresh(self, city_slug: str, max_age_sec: int = 1800) -> bool:
        """Verificar si la observación es reciente (default: última media hora)."""
        obs = self._observations.get(city_slug)
        if not obs or obs.updated_at == 0:
            return False
        return (time.time() - obs.updated_at) < max_age_sec

    def is_temp_declining(self, city_slug: str) -> bool:
        """Verificar si la temperatura actual es menor que el máximo observado.
        Indica que el pico del día ya pasó.
        Requiere al menos 1.5°F de caída para evitar ruido en lecturas METAR.
        """
        obs = self._observations.get(city_slug)
        if not obs or obs.temp_f is None:
            return False
        if obs.observed_high_f <= -999:
            return False
        # La temp actual es al menos 1.5°F menor que el máximo (evitar ruido)
        return obs.temp_f < (obs.observed_high_f - 1.5)

    async def start(self):
        """Loop principal: actualizar observaciones periódicamente."""
        self._running = True
        print(f"[MetarFeed] Iniciando feed METAR para {len(self._cities)} estaciones, "
              f"refresh cada {self._refresh_interval}s", flush=True)

        while self._running:
            try:
                if not self._enabled:
                    await asyncio.sleep(60)
                    continue
                now = time.time()
                if now - self._last_refresh >= self._refresh_interval or self._last_refresh == 0:
                    await self._refresh_all()
                    self._last_refresh = now
                    self._errors_in_a_row = 0
            except Exception as e:
                self._errors_in_a_row += 1
                if self._errors_in_a_row <= 3:
                    print(f"[MetarFeed] Error en refresh: {e}", flush=True)
            await asyncio.sleep(30)  # Check cada 30s si toca refresh

    async def stop(self):
        self._running = False

    async def _refresh_all(self):
        """Actualizar observaciones para todas las estaciones."""
        # Resetear highs si cambió el día (UTC)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._today_str != today:
            self._today_str = today
            for obs in self._observations.values():
                obs.observed_high_c = -999.0
                obs.observed_high_f = -999.0
                obs.observations_today = 0
                obs.high_reached_at = None
            print(f"[MetarFeed] Nuevo día {today}, highs reseteados", flush=True)

        # Hacer un solo request con todas las estaciones juntas
        stations = [c["station"] for c in self._cities]
        station_ids = ",".join(stations)

        updated = 0
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(METAR_API_URL, params={
                    "ids": station_ids,
                    "format": "json",
                })
                if resp.status_code != 200:
                    print(f"[MetarFeed] API error {resp.status_code}", flush=True)
                    return

                data = resp.json()
                if not isinstance(data, list):
                    print(f"[MetarFeed] Respuesta inesperada: {type(data)}", flush=True)
                    return

                for metar in data:
                    try:
                        self._process_metar(metar)
                        updated += 1
                    except Exception as e:
                        station = metar.get("icaoId", "?")
                        print(f"[MetarFeed] Error procesando {station}: {e}", flush=True)

        except Exception as e:
            print(f"[MetarFeed] Request error: {e}", flush=True)
            return

        total_with_high = sum(1 for obs in self._observations.values()
                              if obs.observed_high_f > -999)
        print(f"[MetarFeed] Refresh: {updated}/{len(stations)} estaciones, "
              f"{total_with_high} con high observado", flush=True)

    def _process_metar(self, metar: dict):
        """Procesar un registro METAR y actualizar observación."""
        station = metar.get("icaoId", "")
        if not station:
            return

        # Encontrar ciudad por estación
        city_slug = None
        for city in self._cities:
            if city["station"] == station:
                city_slug = city["slug"]
                break
        if not city_slug:
            return

        # Extraer temperatura
        temp_c = metar.get("temp")
        dewpoint_c = metar.get("dewp")
        wind_speed = metar.get("wspd")
        raw_text = metar.get("rawOb", "")

        if temp_c is None:
            return

        try:
            temp_c = float(temp_c)
        except (ValueError, TypeError):
            return

        # Convertir a Fahrenheit
        temp_f = round(temp_c * 9.0 / 5.0 + 32.0, 1)

        # Parse observation time
        obs_time = None
        report_time = metar.get("reportTime", "")
        if report_time:
            try:
                obs_time = datetime.fromisoformat(report_time.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        # Obtener o crear observación
        if city_slug not in self._observations:
            self._observations[city_slug] = StationObservation(
                station=station,
                city_slug=city_slug,
            )

        obs = self._observations[city_slug]
        obs.temp_c = temp_c
        obs.temp_f = temp_f
        obs.dewpoint_c = float(dewpoint_c) if dewpoint_c is not None else None
        obs.wind_speed_kt = float(wind_speed) if wind_speed is not None else None
        obs.observation_time = obs_time
        obs.raw_metar = raw_text
        obs.updated_at = time.time()
        obs.observations_today += 1

        # Actualizar máximo del día
        if temp_f > obs.observed_high_f:
            obs.observed_high_f = temp_f
            obs.observed_high_c = temp_c
            obs.high_reached_at = obs_time or datetime.now(timezone.utc)

    def get_status(self) -> dict:
        """Estado actual del feed para el dashboard."""
        stations = {}
        for city in self._cities:
            slug = city["slug"]
            obs = self._observations.get(slug)
            if obs:
                stations[slug] = {
                    "station": obs.station,
                    "temp_f": obs.temp_f,
                    "temp_c": obs.temp_c,
                    "observed_high_f": obs.observed_high_f if obs.observed_high_f > -999 else None,
                    "observed_high_c": obs.observed_high_c if obs.observed_high_c > -999 else None,
                    "observations_today": obs.observations_today,
                    "fresh": self.is_observation_fresh(slug),
                    "declining": self.is_temp_declining(slug),
                    "high_reached_at": obs.high_reached_at.isoformat() if obs.high_reached_at else None,
                    "updated_at": obs.updated_at,
                }
            else:
                stations[slug] = {"station": city["station"], "temp_f": None}

        return {
            "running": self._running,
            "enabled": self._enabled,
            "stations": len(self._cities),
            "observations": len(self._observations),
            "last_refresh": self._last_refresh,
            "refresh_interval": self._refresh_interval,
            "station_details": stations,
        }
