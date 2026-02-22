"""Feed de pronósticos meteorológicos desde Open-Meteo Ensemble API.

Equivalente a binance_feed.py del crypto arb, pero en vez de precios
de Binance en tiempo real, consulta pronósticos ensemble (31 miembros GFS)
de Open-Meteo cada 30 minutos.

Cada miembro del ensemble genera una predicción independiente de temperatura
hora a hora. Calculamos la max temp diaria por miembro y generamos una
distribución de probabilidad por rango de temperatura.
"""
import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog
import httpx

logger = structlog.get_logger()

# Open-Meteo Ensemble API — gratuita, sin API key
ENSEMBLE_API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
# Open-Meteo Forecast API — determinístico alta resolución
FORECAST_API_URL = "https://api.open-meteo.com/v1/forecast"


@dataclass
class CityForecast:
    """Pronóstico ensemble para una ciudad en una fecha."""
    city: str                          # Slug de la ciudad (ej: "nyc")
    date: str                          # Fecha ISO (ej: "2026-02-18")
    unit: str                          # "fahrenheit" o "celsius"
    ensemble_max_temps: list[float]    # Max temp por cada miembro (31 valores)
    deterministic_max: Optional[float] = None  # Forecast determinístico (alta res)
    mean_max: float = 0.0              # Media de todos los miembros
    std_max: float = 0.0               # Desviación estándar
    updated_at: float = 0.0            # Timestamp de última actualización
    range_probabilities: dict = field(default_factory=dict)  # "38-39" → 0.35

    @property
    def confidence(self) -> float:
        """Confianza basada en acuerdo del ensemble (0-1).
        Menor std = más acuerdo = más confianza.
        """
        if not self.ensemble_max_temps or self.std_max <= 0:
            return 0.5
        # std < 1°F/C = alta confianza, std > 4 = baja
        return max(0.3, min(1.0, 1.0 - (self.std_max - 0.5) / 6.0))


# Ciudades con mercados de temperatura en Polymarket
# lat/lon son de la estación meteorológica que usa Wunderground para resolución
WEATHER_CITIES = [
    {"slug": "nyc", "name": "New York City", "lat": 40.77, "lon": -73.87,
     "unit": "fahrenheit", "station": "KLGA", "country": "us",
     "wunder_path": "us/ny/new-york-city/KLGA"},
    {"slug": "london", "name": "London", "lat": 51.51, "lon": 0.05,
     "unit": "celsius", "station": "EGLC", "country": "gb",
     "wunder_path": "gb/london/EGLC"},
    {"slug": "chicago", "name": "Chicago", "lat": 41.98, "lon": -87.90,
     "unit": "fahrenheit", "station": "KORD", "country": "us",
     "wunder_path": "us/il/chicago/KORD"},
    {"slug": "dallas", "name": "Dallas", "lat": 32.85, "lon": -96.85,
     "unit": "fahrenheit", "station": "KDFW", "country": "us",
     "wunder_path": "us/tx/dallas/KDFW"},
    {"slug": "atlanta", "name": "Atlanta", "lat": 33.63, "lon": -84.44,
     "unit": "fahrenheit", "station": "KATL", "country": "us",
     "wunder_path": "us/ga/atlanta/KATL"},
    {"slug": "miami", "name": "Miami", "lat": 25.79, "lon": -80.29,
     "unit": "fahrenheit", "station": "KMIA", "country": "us",
     "wunder_path": "us/fl/miami/KMIA"},
    {"slug": "toronto", "name": "Toronto", "lat": 43.68, "lon": -79.63,
     "unit": "celsius", "station": "CYYZ", "country": "ca",
     "wunder_path": "ca/on/toronto/CYYZ"},
    {"slug": "seattle", "name": "Seattle", "lat": 47.45, "lon": -122.31,
     "unit": "fahrenheit", "station": "KSEA", "country": "us",
     "wunder_path": "us/wa/seattle/KSEA"},
    {"slug": "paris", "name": "Paris", "lat": 49.01, "lon": 2.55,
     "unit": "celsius", "station": "LFPG", "country": "fr",
     "wunder_path": "fr/paris/LFPG"},
    {"slug": "seoul", "name": "Seoul", "lat": 37.57, "lon": 127.0,
     "unit": "celsius", "station": "RKSS", "country": "kr",
     "wunder_path": "kr/seoul/RKSS"},
    {"slug": "wellington", "name": "Wellington", "lat": -41.33, "lon": 174.81,
     "unit": "celsius", "station": "NZWN", "country": "nz",
     "wunder_path": "nz/wellington/NZWN"},
    {"slug": "buenos-aires", "name": "Buenos Aires", "lat": -34.56, "lon": -58.54,
     "unit": "celsius", "station": "SAEZ", "country": "ar",
     "wunder_path": "ar/buenos-aires/SAEZ"},
    {"slug": "sao-paulo", "name": "Sao Paulo", "lat": -23.63, "lon": -46.66,
     "unit": "celsius", "station": "SBSP", "country": "br",
     "wunder_path": "br/sao-paulo/SBSP"},
    {"slug": "ankara", "name": "Ankara", "lat": 40.13, "lon": 32.99,
     "unit": "celsius", "station": "LTAC", "country": "tr",
     "wunder_path": "tr/ankara/LTAC"},
]

# Índice rápido por slug
CITY_BY_SLUG = {c["slug"]: c for c in WEATHER_CITIES}


class WeatherFeed:
    """Mantiene pronósticos ensemble actualizados para todas las ciudades.

    Consulta Open-Meteo cada 30 min para obtener distribución probabilística
    de max temperatura por ciudad/fecha. Equivalente a BinanceFeed.
    """

    def __init__(self, cities: Optional[list[str]] = None, refresh_interval: int = 1800):
        """
        Args:
            cities: Lista de slugs de ciudades a monitorear. None = todas.
            refresh_interval: Segundos entre refreshes (default 30 min).
        """
        if cities:
            self._cities = [c for c in WEATHER_CITIES if c["slug"] in cities]
        else:
            self._cities = list(WEATHER_CITIES)
        self._refresh_interval = refresh_interval
        self._forecasts: dict[str, dict[str, CityForecast]] = {}  # city_slug → {date → CityForecast}
        self._last_refresh = 0.0
        self._running = False
        self._errors_in_a_row = 0

    @property
    def is_running(self) -> bool:
        return self._running

    def get_forecast(self, city_slug: str, date: str) -> Optional[CityForecast]:
        """Obtener forecast para ciudad y fecha específica."""
        return self._forecasts.get(city_slug, {}).get(date)

    def get_all_forecasts(self) -> dict[str, dict[str, CityForecast]]:
        """Todos los forecasts actuales."""
        return self._forecasts

    def get_range_probability(self, city_slug: str, date: str,
                               range_label: str) -> Optional[float]:
        """Probabilidad de que max temp caiga en un rango específico.
        range_label: ej "38-39" o "6" o "46 or higher"
        """
        fc = self.get_forecast(city_slug, date)
        if not fc:
            return None
        return fc.range_probabilities.get(range_label)

    async def start(self):
        """Loop principal: actualizar forecasts periódicamente."""
        self._running = True
        print(f"[WeatherFeed] Iniciando feed para {len(self._cities)} ciudades, "
              f"refresh cada {self._refresh_interval}s", flush=True)

        while self._running:
            try:
                now = time.time()
                if now - self._last_refresh >= self._refresh_interval or self._last_refresh == 0:
                    await self._refresh_all()
                    self._last_refresh = now
                    self._errors_in_a_row = 0
            except Exception as e:
                self._errors_in_a_row += 1
                if self._errors_in_a_row <= 3:
                    print(f"[WeatherFeed] Error en refresh: {e}", flush=True)
            await asyncio.sleep(60)  # Check cada minuto si toca refresh

    async def stop(self):
        self._running = False

    async def refresh_now(self):
        """Forzar refresh inmediato (para testing/manual)."""
        await self._refresh_all()
        self._last_refresh = time.time()

    async def _refresh_all(self):
        """Actualizar forecasts para todas las ciudades."""
        updated = 0
        # Fechas a consultar: hoy, mañana, pasado mañana
        now_utc = datetime.now(timezone.utc)
        dates = [(now_utc + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(3)]

        async with httpx.AsyncClient(timeout=20) as client:
            for city in self._cities:
                try:
                    await self._refresh_city(client, city, dates)
                    updated += 1
                except Exception as e:
                    print(f"[WeatherFeed] Error {city['slug']}: {e}", flush=True)
                # Rate limiting: 500ms entre ciudades
                await asyncio.sleep(0.5)

        total_forecasts = sum(len(v) for v in self._forecasts.values())
        print(f"[WeatherFeed] Refresh: {updated}/{len(self._cities)} ciudades, "
              f"{total_forecasts} forecasts activos", flush=True)

    async def _refresh_city(self, client: httpx.AsyncClient,
                             city: dict, dates: list[str]):
        """Actualizar forecast ensemble para una ciudad."""
        slug = city["slug"]
        lat = city["lat"]
        lon = city["lon"]
        unit = city["unit"]
        temp_unit = unit  # "fahrenheit" o "celsius"

        # Consultar ensemble (31 miembros GFS)
        resp = await client.get(ENSEMBLE_API_URL, params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "models": "gfs_seamless",
            "forecast_days": 3,
            "temperature_unit": temp_unit,
        })
        if resp.status_code != 200:
            print(f"[WeatherFeed] Ensemble API error {resp.status_code} para {slug}", flush=True)
            return

        data = resp.json()
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])

        # Obtener todos los miembros del ensemble
        members = {k: v for k, v in hourly.items() if k.startswith("temperature_2m")}
        if not members:
            return

        # Consultar forecast determinístico (alta resolución) como referencia
        det_max_by_date = {}
        try:
            resp2 = await client.get(FORECAST_API_URL, params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max",
                "forecast_days": 3,
                "temperature_unit": temp_unit,
            })
            if resp2.status_code == 200:
                det_data = resp2.json()
                det_dates = det_data.get("daily", {}).get("time", [])
                det_maxs = det_data.get("daily", {}).get("temperature_2m_max", [])
                for d, m in zip(det_dates, det_maxs):
                    if m is not None:
                        det_max_by_date[d] = m
        except Exception:
            pass

        # Procesar por fecha
        if slug not in self._forecasts:
            self._forecasts[slug] = {}

        for date_str in dates:
            # Filtrar horas de esta fecha por miembro
            max_temps = []
            for member_key, vals in members.items():
                day_vals = [
                    v for t, v in zip(times, vals)
                    if t.startswith(date_str) and v is not None
                ]
                if day_vals:
                    max_temps.append(max(day_vals))

            if not max_temps:
                continue

            # Estadísticas
            mean_val = sum(max_temps) / len(max_temps)
            variance = sum((t - mean_val) ** 2 for t in max_temps) / len(max_temps)
            std_val = variance ** 0.5

            forecast = CityForecast(
                city=slug,
                date=date_str,
                unit=unit,
                ensemble_max_temps=max_temps,
                deterministic_max=det_max_by_date.get(date_str),
                mean_max=round(mean_val, 1),
                std_max=round(std_val, 2),
                updated_at=time.time(),
            )

            self._forecasts[slug][date_str] = forecast

    def compute_range_probabilities(self, forecast: CityForecast,
                                     ranges: list[dict]) -> dict[str, float]:
        """Calcular probabilidad ensemble para cada rango de temperatura.

        Polymarket resuelve con la temperatura HIGH reportada por Weather
        Underground, que es un entero redondeado. Por eso redondeamos cada
        miembro del ensemble antes de asignar bucket.

        Además, el ensemble GFS tiene un sesgo frío sistemático de ~0.5-1.5°C
        respecto al forecast determinístico. Si tenemos el determinístico,
        ajustamos los miembros para corregir el bias.

        Args:
            forecast: CityForecast con ensemble_max_temps
            ranges: Lista de dicts con "label", "low", "high" del mercado Polymarket

        Returns:
            dict: label → probabilidad (0-1)
        """
        if not forecast.ensemble_max_temps or not ranges:
            return {}

        temps = list(forecast.ensemble_max_temps)
        n = len(temps)

        # ── Corrección de bias con forecast determinístico ──
        # El ensemble GFS tiende a subestimar la max temp vs el modelo
        # determinístico de alta resolución. Aplicamos shift si disponible.
        if forecast.deterministic_max is not None and n > 0:
            ens_mean = sum(temps) / n
            bias = forecast.deterministic_max - ens_mean
            # Solo corregir si el bias es positivo (ensemble frío) y razonable
            if 0.2 < bias < 4.0:
                temps = [t + bias for t in temps]

        # ── Redondear al entero más cercano (como WU reporta) ──
        rounded = [round(t) for t in temps]

        probs = {}
        for r in ranges:
            label = r["label"]
            low = r.get("low", float("-inf"))
            high = r.get("high", float("inf"))
            # Rangos abiertos (or below / or higher): inclusivo
            # Rangos cerrados (X-Y): low <= round(t) <= high
            if high == 999 or high == float("inf"):
                count = sum(1 for t in rounded if t >= low)
            elif low == -999 or low == float("-inf"):
                count = sum(1 for t in rounded if t <= high)
            else:
                count = sum(1 for t in rounded if low <= t <= high)
            probs[label] = count / n

        # Guardar en el forecast
        forecast.range_probabilities = probs
        return probs

    def get_status(self) -> dict:
        """Estado actual del feed para el dashboard."""
        cities_status = {}
        for city in self._cities:
            slug = city["slug"]
            fc_dates = self._forecasts.get(slug, {})
            cities_status[slug] = {
                "name": city["name"],
                "unit": city["unit"],
                "forecasts": len(fc_dates),
                "dates": list(fc_dates.keys()),
            }
            # Agregar resumen del forecast más cercano
            for date_str, fc in fc_dates.items():
                cities_status[slug][f"mean_{date_str}"] = fc.mean_max
                cities_status[slug][f"std_{date_str}"] = fc.std_max
                cities_status[slug][f"confidence_{date_str}"] = round(fc.confidence, 2)

        return {
            "running": self._running,
            "cities": len(self._cities),
            "total_forecasts": sum(len(v) for v in self._forecasts.values()),
            "last_refresh": self._last_refresh,
            "refresh_interval": self._refresh_interval,
            "city_details": cities_status,
        }
