"""Feed de datos desde Weather Underground — fuente de resolución de Polymarket.

WU es la fuente que Polymarket usa para resolver mercados de temperatura.
Si WU reporta 48°F como máxima del día, el mercado resuelve a 48°F.
Esto nos da ~99% de certeza en la resolución.

Usa la API de The Weather Company (TWC) que WU consume internamente.
API key configurable via env var WU_API_KEY.
"""
import asyncio
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import structlog
import httpx

from src.weather_arb.weather_feed import WEATHER_CITIES

logger = structlog.get_logger()

# TWC API endpoints (misma API que usa WU internamente)
CURRENT_OBS_API = "https://api.weather.com/v3/wx/observations/current"
HISTORY_OBS_API = "https://api.weather.com/v1/location/{icao}:9:{cc}/observations/historical.json"

# API key default (embebida en el JS de wunderground.com)
DEFAULT_WU_API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"


@dataclass
class WuObservation:
    """Observación de WU para una ciudad."""
    city_slug: str
    station: str
    high_temp_f: Optional[float] = None   # Max temp en Fahrenheit
    high_temp_c: Optional[float] = None   # Max temp en Celsius
    current_temp_f: Optional[float] = None
    current_temp_c: Optional[float] = None
    observation_time_utc: Optional[float] = None  # epoch
    source: str = ""  # "current_api" o "history_api"
    fetched_at: float = 0.0  # epoch cuando se obtuvo
    raw_data: dict = field(default_factory=dict)
    error: Optional[str] = None


class WundergroundFeed:
    """Scraper de Weather Underground — fuente de resolución real de Polymarket.

    Consulta la API de The Weather Company (TWC) que WU usa internamente
    para obtener la temperatura máxima del día (temperatureMaxSince7Am).
    """

    def __init__(self, api_key: str = "", refresh_sec: int = 300,
                 cities: Optional[list[str]] = None):
        """
        Args:
            api_key: API key de TWC/WU. Si vacía, usa env WU_API_KEY o default.
            refresh_sec: Segundos entre refreshes (default 5 min).
            cities: Lista de slugs a monitorear. None = todas.
        """
        self._api_key = api_key or os.getenv("WU_API_KEY", DEFAULT_WU_API_KEY)
        self._refresh_sec = refresh_sec
        if cities:
            self._cities = [c for c in WEATHER_CITIES if c["slug"] in cities]
        else:
            self._cities = list(WEATHER_CITIES)
        self._cache: dict[str, WuObservation] = {}  # city_slug → WuObservation
        self._running = False
        self._enabled = True
        self._client: Optional[httpx.AsyncClient] = None
        self._last_refresh: dict[str, float] = {}  # city_slug → epoch
        self._errors: dict[str, int] = {}  # city_slug → consecutive errors
        self._today_str = ""

    async def start(self):
        """Loop principal: refrescar datos para todas las ciudades."""
        self._running = True
        self._client = httpx.AsyncClient(
            timeout=15.0,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
            },
            follow_redirects=True,
        )
        logger.info("wu_feed_started", api_key_len=len(self._api_key))
        print(f"[WU Feed] Iniciado — {len(self._cities)} ciudades, "
              f"refresh cada {self._refresh_sec}s, "
              f"API key: {self._api_key[:8]}...{self._api_key[-4:]}",
              flush=True)

        while self._running:
            try:
                if not self._enabled:
                    await asyncio.sleep(60)
                    continue
                await self._refresh_all()
            except Exception as e:
                logger.warning("wu_feed_refresh_error", error=str(e))
                print(f"[WU Feed] Error general en refresh: {e}", flush=True)
            await asyncio.sleep(self._refresh_sec)

    async def stop(self):
        self._running = False
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _refresh_all(self):
        """Refrescar datos de todas las ciudades."""
        # Resetear cache si cambió el día (UTC)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._today_str != today:
            self._today_str = today
            self._cache.clear()
            self._last_refresh.clear()
            print(f"[WU Feed] Nuevo día {today}, cache reseteado", flush=True)

        now = time.time()
        updated = 0
        errors = 0
        for city in self._cities:
            slug = city["slug"]
            last = self._last_refresh.get(slug, 0)
            if now - last < self._refresh_sec:
                continue
            try:
                obs = await self._fetch_city(city)
                if obs and not obs.error:
                    self._cache[slug] = obs
                    self._last_refresh[slug] = now
                    self._errors[slug] = 0
                    updated += 1
                else:
                    self._errors[slug] = self._errors.get(slug, 0) + 1
                    errors += 1
                    if obs and obs.error and self._errors[slug] <= 2:
                        print(f"[WU Feed] ⚠ {city['name']}: {obs.error}", flush=True)
            except Exception as e:
                self._errors[slug] = self._errors.get(slug, 0) + 1
                errors += 1
                if self._errors[slug] <= 2:
                    print(f"[WU Feed] ⚠ Exception {slug}: {e}", flush=True)
            # Rate limiting: 0.5s entre requests
            await asyncio.sleep(0.5)

        if updated > 0 or errors > 0:
            cached = len(self._cache)
            print(f"[WU Feed] Refresh: {updated} OK, {errors} errores, "
                  f"{cached}/{len(self._cities)} en cache", flush=True)

    async def _fetch_city(self, city: dict) -> Optional[WuObservation]:
        """Obtener observación de WU para una ciudad.
        Intenta current API primero, luego history API como fallback.
        """
        if not self._client:
            return None
        station = city["station"]
        slug = city["slug"]
        unit_param = "e" if city["unit"] == "fahrenheit" else "m"

        # Intentar current observations API primero
        obs = await self._try_current_api(station, slug, unit_param, city["unit"])
        if obs and not obs.error:
            return obs

        # Fallback: history API
        obs2 = await self._try_history_api(city)
        if obs2 and not obs2.error:
            return obs2

        # Ambos fallaron
        err1 = obs.error if obs else "no response"
        err2 = obs2.error if obs2 else "no response"
        return WuObservation(
            city_slug=slug, station=station,
            error=f"current: {err1} | history: {err2}"
        )

    async def _try_current_api(self, station: str, slug: str,
                                unit_param: str, unit: str) -> Optional[WuObservation]:
        """Intentar v3/wx/observations/current API.
        Retorna temperatureMaxSince7Am = máxima del día según WU.
        """
        try:
            params = {
                "icaoCode": station,
                "language": "en-US",
                "units": unit_param,
                "format": "json",
                "apiKey": self._api_key,
            }
            resp = await self._client.get(CURRENT_OBS_API, params=params)
            if resp.status_code != 200:
                return WuObservation(
                    city_slug=slug, station=station,
                    error=f"current_api HTTP {resp.status_code}"
                )
            data = resp.json()

            obs = WuObservation(
                city_slug=slug,
                station=station,
                source="current_api",
                fetched_at=time.time(),
                raw_data=data,
            )

            # Extraer temperaturas según unidad solicitada
            high_temp = data.get("temperatureMaxSince7Am")
            current_temp = data.get("temperature")
            # Fallback si no hay maxSince7Am
            if high_temp is None:
                high_temp = data.get("temperatureMax24Hour")

            if unit == "fahrenheit":
                obs.high_temp_f = float(high_temp) if high_temp is not None else None
                obs.current_temp_f = float(current_temp) if current_temp is not None else None
            else:
                obs.high_temp_c = float(high_temp) if high_temp is not None else None
                obs.current_temp_c = float(current_temp) if current_temp is not None else None

            # Guardar observation time
            valid_utc = data.get("validTimeUtc")
            if valid_utc:
                obs.observation_time_utc = float(valid_utc)

            high_display = high_temp if high_temp is not None else "?"
            curr_display = current_temp if current_temp is not None else "?"
            unit_symbol = "F" if unit == "fahrenheit" else "C"
            print(f"[WU Feed] ✓ {slug}: high={high_display}°{unit_symbol} "
                  f"current={curr_display}°{unit_symbol} (current_api)",
                  flush=True)

            return obs
        except httpx.TimeoutException:
            return WuObservation(
                city_slug=slug, station=station,
                error="current_api: timeout"
            )
        except Exception as e:
            return WuObservation(
                city_slug=slug, station=station,
                error=f"current_api: {str(e)[:80]}"
            )

    async def _try_history_api(self, city: dict) -> Optional[WuObservation]:
        """Fallback: v1 history API — obtener max de observaciones horarias del día."""
        station = city["station"]
        slug = city["slug"]
        cc = city["country"].upper()
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(city["tz"])
            local_now = datetime.now(tz)
            date_str = local_now.strftime("%Y%m%d")

            url = HISTORY_OBS_API.format(icao=station, cc=cc)
            params = {
                "apiKey": self._api_key,
                "units": "e" if city["unit"] == "fahrenheit" else "m",
                "startDate": date_str,
                "endDate": date_str,
            }
            resp = await self._client.get(url, params=params)
            if resp.status_code != 200:
                return WuObservation(
                    city_slug=slug, station=station,
                    error=f"history_api HTTP {resp.status_code}"
                )

            data = resp.json()
            observations = data.get("observations", [])
            if not observations:
                return WuObservation(
                    city_slug=slug, station=station,
                    error="history_api: sin observaciones"
                )

            # Encontrar temperatura máxima de todas las observaciones del día
            temps = [o.get("temp") for o in observations if o.get("temp") is not None]
            if not temps:
                return WuObservation(
                    city_slug=slug, station=station,
                    error="history_api: sin temps válidas"
                )

            max_temp = max(float(t) for t in temps)
            current_temp = float(temps[-1])

            obs = WuObservation(
                city_slug=slug,
                station=station,
                source="history_api",
                fetched_at=time.time(),
                raw_data={"observations_count": len(observations), "max_temp": max_temp},
            )

            if city["unit"] == "fahrenheit":
                obs.high_temp_f = max_temp
                obs.current_temp_f = current_temp
            else:
                obs.high_temp_c = max_temp
                obs.current_temp_c = current_temp

            unit_symbol = "F" if city["unit"] == "fahrenheit" else "C"
            print(f"[WU Feed] ✓ {slug}: high={max_temp:.0f}°{unit_symbol} "
                  f"({len(observations)} obs) (history_api fallback)",
                  flush=True)

            return obs
        except httpx.TimeoutException:
            return WuObservation(
                city_slug=slug, station=station,
                error="history_api: timeout"
            )
        except Exception as e:
            return WuObservation(
                city_slug=slug, station=station,
                error=f"history_api: {str(e)[:80]}"
            )

    # ── Public API ──────────────────────────────────────────────────────

    def get_wu_high(self, city_slug: str, unit: str = "fahrenheit") -> Optional[float]:
        """Obtener la temperatura máxima del día según WU para una ciudad.

        Args:
            city_slug: Slug de la ciudad (ej: "nyc")
            unit: "fahrenheit" o "celsius"

        Returns:
            Temperatura máxima en la unidad especificada, o None si no hay datos.
        """
        obs = self._cache.get(city_slug)
        if not obs:
            return None
        if unit == "fahrenheit":
            return obs.high_temp_f
        return obs.high_temp_c

    def get_wu_current(self, city_slug: str, unit: str = "fahrenheit") -> Optional[float]:
        """Obtener la temperatura actual según WU."""
        obs = self._cache.get(city_slug)
        if not obs:
            return None
        if unit == "fahrenheit":
            return obs.current_temp_f
        return obs.current_temp_c

    def is_wu_fresh(self, city_slug: str, max_age_sec: int = 600) -> bool:
        """Verificar si los datos de WU son recientes (default 10 min)."""
        obs = self._cache.get(city_slug)
        if not obs:
            return False
        return (time.time() - obs.fetched_at) < max_age_sec

    def get_wu_observation(self, city_slug: str) -> Optional[WuObservation]:
        """Obtener la observación completa de WU."""
        return self._cache.get(city_slug)

    def get_status(self) -> dict:
        """Estado actual del feed para dashboard."""
        cities_ok = sum(1 for obs in self._cache.values() if not obs.error)
        cities_err = sum(1 for slug, errs in self._errors.items() if errs > 0)
        return {
            "running": self._running,
            "enabled": self._enabled,
            "cities_total": len(self._cities),
            "cities_cached": len(self._cache),
            "cities_ok": cities_ok,
            "cities_errors": cities_err,
            "api_key_set": bool(self._api_key),
            "refresh_sec": self._refresh_sec,
            "cache": {
                slug: {
                    "high_f": obs.high_temp_f,
                    "high_c": obs.high_temp_c,
                    "current_f": obs.current_temp_f,
                    "current_c": obs.current_temp_c,
                    "source": obs.source,
                    "age_sec": round(time.time() - obs.fetched_at) if obs.fetched_at else None,
                    "error": obs.error,
                }
                for slug, obs in self._cache.items()
            }
        }
