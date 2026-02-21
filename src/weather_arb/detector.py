"""Detector de oportunidades de arbitraje en mercados weather de Polymarket.

Escanea mercados de "Highest temperature in {city} on {date}?" y compara
los odds de Polymarket contra la distribución probabilística del ensemble
meteorológico (Open-Meteo GFS 31 miembros).

Equivalente a crypto_arb/detector.py pero para clima en vez de crypto.
"""
import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog
import httpx

from src import config
from src.weather_arb.weather_feed import WeatherFeed, CITY_BY_SLUG, WEATHER_CITIES

logger = structlog.get_logger()

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Meses en inglés para generar slugs
MONTH_NAMES = {
    1: "january", 2: "february", 3: "march", 4: "april",
    5: "may", 6: "june", 7: "july", 8: "august",
    9: "september", 10: "october", 11: "november", 12: "december",
}


@dataclass
class WeatherSignal:
    """Una señal de arbitraje weather detectada."""
    city: str                    # Slug de la ciudad (ej: "nyc")
    city_name: str               # Nombre completo (ej: "New York City")
    date: str                    # Fecha ISO (ej: "2026-02-18")
    range_label: str             # Rango ganador (ej: "38-39°F")
    ensemble_prob: float         # Probabilidad real del ensemble (0-1)
    poly_odds: float             # Odds actuales en Polymarket (0-1)
    edge_pct: float              # Edge = (ensemble_prob - poly_odds) * 100
    confidence: float            # Confianza del ensemble (0-100)
    condition_id: str            # ID del mercado en Polymarket
    market_question: str         # Pregunta del mercado
    event_slug: str              # Slug del evento
    mean_temp: float             # Temp media del ensemble
    std_temp: float              # Desviación estándar
    ensemble_members: int        # Número de miembros (31)
    unit: str                    # "fahrenheit" o "celsius"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved: bool = False
    result: Optional[str] = None  # "win" o "loss"
    paper_pnl: float = 0.0
    token_id: str = ""           # Token ID del outcome (Yes para conviction, No para elimination)
    strategy: str = "conviction"  # "conviction" (BUY YES) o "elimination" (BUY NO)
    resolution_source: str = ""  # URL de Wunderground

    @property
    def expected_profit_pct(self) -> float:
        """Profit esperado si compramos a poly_odds y gana."""
        if self.poly_odds <= 0:
            return 0
        return ((1.0 / self.poly_odds) - 1) * 100


class WeatherArbDetector:
    """Detecta oportunidades de arbitraje en mercados weather de Polymarket."""

    def __init__(self, weather_feed: WeatherFeed, multi_feed=None):
        self.feed = weather_feed
        self.multi_feed = multi_feed  # MultiWeatherFeed opcional
        self._active_markets: dict[str, dict] = {}  # event_slug → market_data
        self._last_scan = 0.0
        self._signals_today: list[WeatherSignal] = []
        self._signals_today_date: str = ""
        self._running = False
        # Config — valores por defecto, se sobreescriben desde config.py
        self._min_edge = 8.0          # Edge mínimo para señal (%)
        self._min_confidence = 50.0   # Confianza mínima del ensemble (%)
        self._max_poly_odds = 0.85    # No comprar si odds ya muy alto
        self._scan_interval = 300     # Escanear mercados cada 5 min
        self._enabled_cities: Optional[list[str]] = None  # None = todas
        # Elimination strategy
        self._elimination_enabled = False
        self._elimination_min_profit = 2.0  # % mínimo de profit
        self._elimination_max_bet = 50      # Máximo por trade de eliminación
        self._elimination_require_zero = True  # Requerir 0/31 miembros en rango

    def configure(self, cfg: dict):
        """Actualizar configuración desde config.py o DB."""
        self._min_edge = cfg.get("min_edge", self._min_edge)
        self._min_confidence = cfg.get("min_confidence", self._min_confidence)
        self._max_poly_odds = cfg.get("max_poly_odds", self._max_poly_odds)
        self._scan_interval = cfg.get("scan_interval", self._scan_interval)
        self._enabled_cities = cfg.get("enabled_cities", self._enabled_cities)
        # Elimination strategy
        self._elimination_enabled = cfg.get("elimination_enabled", self._elimination_enabled)
        self._elimination_min_profit = cfg.get("elimination_min_profit", self._elimination_min_profit)
        self._elimination_max_bet = cfg.get("elimination_max_bet", self._elimination_max_bet)
        self._elimination_require_zero = cfg.get("elimination_require_zero", self._elimination_require_zero)

    async def start(self):
        """Loop principal: escanear mercados y detectar divergencias."""
        self._running = True
        logger.info("weather_arb_detector_started")

        while self._running:
            try:
                now = time.time()
                if now - self._last_scan > self._scan_interval:
                    await self._scan_active_markets()
                    self._last_scan = now

                # Buscar señales de convicción (comprar YES)
                signals = await self._check_edges()
                for signal in signals:
                    self._record_signal(signal)

                # Buscar señales de eliminación (comprar NO)
                if self._elimination_enabled:
                    elim_signals = await self._check_elimination_edges()
                    for signal in elim_signals:
                        self._record_signal(signal)

            except Exception as e:
                logger.warning("weather_arb_error", error=str(e))
                import traceback
                traceback.print_exc()

            await asyncio.sleep(30)  # Check cada 30 segundos

    async def stop(self):
        self._running = False

    async def _scan_active_markets(self):
        """Buscar mercados weather activos en Polymarket por slug calculado.

        Los slugs siguen el patrón:
          highest-temperature-in-{city}-on-{month}-{day}-{year}
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                now_utc = datetime.now(timezone.utc)
                new_active = {}
                slugs_checked = 0

                # Generar slugs para hoy, mañana y pasado mañana
                cities = self._cities_to_scan()
                for city in cities:
                    for day_offset in range(3):
                        target_date = now_utc + timedelta(days=day_offset)
                        month_name = MONTH_NAMES[target_date.month]
                        day = target_date.day
                        year = target_date.year
                        date_str = target_date.strftime("%Y-%m-%d")

                        slug = f"highest-temperature-in-{city['slug']}-on-{month_name}-{day}-{year}"
                        slugs_checked += 1

                        try:
                            resp = await client.get(
                                f"{GAMMA_API}/events",
                                params={"slug": slug},
                            )
                            if resp.status_code != 200:
                                continue
                            events = resp.json()
                            if not events:
                                continue

                            ev = events[0]
                            if ev.get("closed") or not ev.get("active"):
                                continue

                            # Parsear mercados (rangos de temperatura)
                            markets = ev.get("markets", [])
                            ranges = []
                            for m in markets:
                                if m.get("closed"):
                                    continue
                                cid = m.get("conditionId", "")
                                if not cid:
                                    continue

                                group_title = m.get("groupItemTitle", "")
                                range_info = self._parse_range(group_title, city["unit"])
                                if not range_info:
                                    continue

                                # Obtener odds actuales
                                outcome_prices = m.get("outcomePrices", "[]")
                                try:
                                    prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                                    yes_price = float(prices[0]) if prices else 0
                                except (json.JSONDecodeError, IndexError, ValueError):
                                    yes_price = 0

                                # Obtener token IDs (YES = [0], NO = [1])
                                clob_token_ids = m.get("clobTokenIds", "[]")
                                try:
                                    token_ids = json.loads(clob_token_ids) if isinstance(clob_token_ids, str) else clob_token_ids
                                    yes_token = token_ids[0] if token_ids else ""
                                    no_token = token_ids[1] if len(token_ids) > 1 else ""
                                except (json.JSONDecodeError, IndexError):
                                    yes_token = ""
                                    no_token = ""

                                ranges.append({
                                    "condition_id": cid,
                                    "label": group_title,
                                    "low": range_info["low"],
                                    "high": range_info["high"],
                                    "yes_price": yes_price,
                                    "yes_token": yes_token,
                                    "no_token": no_token,
                                    "question": m.get("question", ""),
                                    "volume": float(m.get("volume", 0)),
                                })

                            if ranges:
                                new_active[slug] = {
                                    "event_slug": slug,
                                    "city": city,
                                    "date": date_str,
                                    "ranges": ranges,
                                    "title": ev.get("title", ""),
                                    "volume": float(ev.get("volume", 0)),
                                    "resolution_source": ev.get("resolutionSource", ""),
                                    "end_date": ev.get("endDate", ""),
                                }

                        except Exception:
                            pass

                        # Rate limiting entre requests
                        await asyncio.sleep(0.3)

                self._active_markets = new_active

                print(f"[WeatherDetector] Scan: {slugs_checked} slugs verificados, "
                      f"{len(new_active)} mercados weather activos", flush=True)
                for slug, md in list(new_active.items())[:8]:
                    city_name = md["city"]["name"]
                    n_ranges = len(md["ranges"])
                    vol = md["volume"]
                    print(f"  → {city_name} {md['date']}: {n_ranges} rangos, vol=${vol:,.0f}", flush=True)

        except Exception as e:
            print(f"[WeatherDetector] Scan error: {e}", flush=True)
            import traceback
            traceback.print_exc()

    def _cities_to_scan(self) -> list[dict]:
        """Obtener lista de ciudades a escanear."""
        if self._enabled_cities:
            return [c for c in WEATHER_CITIES if c["slug"] in self._enabled_cities]
        return list(WEATHER_CITIES)

    def _parse_range(self, label: str, unit: str) -> Optional[dict]:
        """Parsear un label de rango de Polymarket a low/high numéricos.

        Ejemplos:
          "38-39°F" → {"low": 38, "high": 39}
          "6°C" → {"low": 6, "high": 6}
          "31°F or below" → {"low": -999, "high": 31}
          "46°F or higher" → {"low": 46, "high": 999}
          "1°C or below" → {"low": -999, "high": 1}
          "9°C or higher" → {"low": 9, "high": 999}
        """
        if not label:
            return None

        # Limpiar
        clean = label.replace("°F", "").replace("°C", "").strip()

        # "X or below"
        m = re.match(r"(-?\d+)\s+or\s+below", clean, re.IGNORECASE)
        if m:
            return {"low": -999, "high": int(m.group(1))}

        # "X or higher" / "X or above"
        m = re.match(r"(-?\d+)\s+or\s+(higher|above)", clean, re.IGNORECASE)
        if m:
            return {"low": int(m.group(1)), "high": 999}

        # "X-Y" (rango)
        m = re.match(r"(-?\d+)\s*-\s*(-?\d+)", clean)
        if m:
            return {"low": int(m.group(1)), "high": int(m.group(2))}

        # Número solo: "6" = rango de 1 grado
        m = re.match(r"(-?\d+)$", clean)
        if m:
            val = int(m.group(1))
            return {"low": val, "high": val}

        return None

    async def _check_edges(self) -> list[WeatherSignal]:
        """Comparar forecasts ensemble vs odds de Polymarket."""
        signals = []
        now = datetime.now(timezone.utc)

        # Reset diario de señales
        today_str = now.strftime("%Y-%m-%d")
        if self._signals_today_date != today_str:
            self._signals_today = []
            self._signals_today_date = today_str

        for slug, mdata in self._active_markets.items():
            city = mdata["city"]
            city_slug = city["slug"]
            date_str = mdata["date"]
            ranges = mdata["ranges"]

            # Obtener forecast del ensemble
            forecast = self.feed.get_forecast(city_slug, date_str)
            if not forecast or not forecast.ensemble_max_temps:
                continue

            # Calcular probabilidades por rango
            range_defs = [{"label": r["label"], "low": r["low"], "high": r["high"]} for r in ranges]
            probs = self.feed.compute_range_probabilities(forecast, range_defs)
            if not probs:
                continue

            # Buscar edges en cada rango
            for r in ranges:
                label = r["label"]
                ensemble_prob = probs.get(label, 0)
                poly_odds = r["yes_price"]

                # Filtros básicos
                if ensemble_prob < 0.05:
                    continue  # Probabilidad muy baja, no vale la pena
                if poly_odds <= 0 or poly_odds >= 1:
                    continue
                if poly_odds > self._max_poly_odds:
                    continue  # Odds ya muy alto

                edge_pct = (ensemble_prob - poly_odds) * 100
                # Confianza base del ensemble + boost de multi-source
                boost = self._get_confidence_boost(city_slug, date_str)
                confidence = min(100, (forecast.confidence + boost) * 100)

                # ¿Hay edge suficiente?
                if edge_pct < self._min_edge:
                    continue
                if confidence < self._min_confidence:
                    continue

                # Evitar duplicados
                cid = r["condition_id"]
                already = any(s.condition_id == cid for s in self._signals_today)
                if already:
                    # Actualizar señal existente en vez de duplicar
                    for s in self._signals_today:
                        if s.condition_id == cid:
                            s.ensemble_prob = ensemble_prob
                            s.poly_odds = poly_odds
                            s.edge_pct = edge_pct
                            s.confidence = confidence
                            s.timestamp = now
                            break
                    continue

                signal = WeatherSignal(
                    city=city_slug,
                    city_name=city["name"],
                    date=date_str,
                    range_label=label,
                    ensemble_prob=round(ensemble_prob, 4),
                    poly_odds=round(poly_odds, 4),
                    edge_pct=round(edge_pct, 2),
                    confidence=round(confidence, 1),
                    condition_id=cid,
                    market_question=r["question"],
                    event_slug=slug,
                    mean_temp=forecast.mean_max,
                    std_temp=forecast.std_max,
                    ensemble_members=len(forecast.ensemble_max_temps),
                    unit=city["unit"],
                    token_id=r.get("yes_token", ""),
                    strategy="conviction",
                    resolution_source=mdata.get("resolution_source", ""),
                )
                signals.append(signal)

        return signals

    async def _check_elimination_edges(self) -> list[WeatherSignal]:
        """Buscar rangos con 0% ensemble pero >3¢ en Polymarket → comprar NO.

        Ejemplo: Buenos Aires Feb 22, ensemble dice 30-34°C.
        Si "20°C or below" tiene YES=5¢, comprar NO a 95¢ → +5.3% profit
        porque NINGÚN miembro del ensemble predice ≤20°C.
        """
        signals = []
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        if self._signals_today_date != today_str:
            self._signals_today = []
            self._signals_today_date = today_str

        for slug, mdata in self._active_markets.items():
            city = mdata["city"]
            city_slug = city["slug"]
            date_str = mdata["date"]
            ranges = mdata["ranges"]

            forecast = self.feed.get_forecast(city_slug, date_str)
            if not forecast or not forecast.ensemble_max_temps:
                continue

            range_defs = [{"label": r["label"], "low": r["low"], "high": r["high"]} for r in ranges]
            probs = self.feed.compute_range_probabilities(forecast, range_defs)
            if not probs:
                continue

            n_members = len(forecast.ensemble_max_temps)

            for r in ranges:
                label = r["label"]
                ensemble_prob = probs.get(label, 0)
                poly_yes = r["yes_price"]

                # Eliminación: ensemble dice 0% (o casi) pero Poly paga >3¢ por YES
                if self._elimination_require_zero and ensemble_prob > 0:
                    continue
                if not self._elimination_require_zero and ensemble_prob > 0.03:
                    continue

                if poly_yes <= 0.02:
                    continue  # YES ya está a 2¢ o menos, no hay profit
                if poly_yes >= 0.15:
                    continue  # YES muy alto, riesgoso para elimination

                # Profit de comprar NO: pagamos (1 - poly_yes), recibimos $1
                no_price = 1.0 - poly_yes
                profit_pct = ((1.0 / no_price) - 1.0) * 100

                if profit_pct < self._elimination_min_profit:
                    continue

                # Verificar confianza
                confidence = forecast.confidence * 100
                if confidence < 40:
                    continue

                # Evitar duplicados
                cid = r["condition_id"]
                already = any(s.condition_id == cid for s in self._signals_today)
                if already:
                    continue

                # Contar cuántos miembros caen en este rango
                low = r.get("low", float("-inf"))
                high = r.get("high", float("inf"))
                if high == 999 or high == float("inf"):
                    members_in_range = sum(1 for t in forecast.ensemble_max_temps if t >= low)
                elif low == -999 or low == float("-inf"):
                    members_in_range = sum(1 for t in forecast.ensemble_max_temps if t <= high)
                else:
                    members_in_range = sum(1 for t in forecast.ensemble_max_temps if low <= t < high + 1)

                signal = WeatherSignal(
                    city=city_slug,
                    city_name=city["name"],
                    date=date_str,
                    range_label=f"ELIM: NO on {label}",
                    ensemble_prob=round(1.0 - ensemble_prob, 4),  # Prob del NO
                    poly_odds=round(no_price, 4),  # Precio del NO
                    edge_pct=round(profit_pct, 2),
                    confidence=round(confidence, 1),
                    condition_id=cid,
                    market_question=r["question"],
                    event_slug=slug,
                    mean_temp=forecast.mean_max,
                    std_temp=forecast.std_max,
                    ensemble_members=n_members,
                    unit=city["unit"],
                    token_id=r.get("no_token", ""),  # NO token para eliminación
                    strategy="elimination",
                    resolution_source=mdata.get("resolution_source", ""),
                )
                signals.append(signal)

        return signals

    def _get_confidence_boost(self, city_slug: str, date_str: str) -> float:
        """Obtener boost de confianza desde multi-source feed."""
        if not self.multi_feed:
            return 0.0
        consensus = self.multi_feed.get_consensus(city_slug, date_str)
        if not consensus:
            return 0.0
        return consensus.confidence_boost

    def _record_signal(self, signal: WeatherSignal):
        """Registrar señal en la lista del día."""
        self._signals_today.append(signal)
        is_elim = signal.range_label.startswith("ELIM:")
        emoji = "🚫" if is_elim else "🌡️"
        stype = "ELIMINACIÓN" if is_elim else "SEÑAL"
        print(f"[WeatherDetector] {emoji} {stype}: {signal.city_name} {signal.date} "
              f"→ {signal.range_label} | ensemble={signal.ensemble_prob:.0%} "
              f"poly={signal.poly_odds:.0%} edge={signal.edge_pct:.1f}% "
              f"conf={signal.confidence:.0f}%", flush=True)

    def get_recent_signals(self, limit: int = 50) -> list[dict]:
        """Señales recientes como dicts para API/dashboard."""
        signals = sorted(self._signals_today, key=lambda s: s.edge_pct, reverse=True)
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
                "mean_temp": s.mean_temp,
                "std_temp": s.std_temp,
                "ensemble_members": s.ensemble_members,
                "unit": s.unit,
                "token_id": s.token_id,
                "strategy": s.strategy,
                "timestamp": s.timestamp.isoformat(),
                "resolved": s.resolved,
                "result": s.result,
                "paper_pnl": s.paper_pnl,
                "expected_profit_pct": round(s.expected_profit_pct, 1),
                "resolution_source": s.resolution_source,
            }
            for s in signals[:limit]
        ]

    def get_active_markets(self) -> list[dict]:
        """Mercados activos para el dashboard."""
        result = []
        for slug, mdata in self._active_markets.items():
            city = mdata["city"]
            forecast = self.feed.get_forecast(city["slug"], mdata["date"])
            ranges_info = []
            for r in mdata["ranges"]:
                prob = forecast.range_probabilities.get(r["label"], 0) if forecast else 0
                ranges_info.append({
                    "label": r["label"],
                    "poly_odds": r["yes_price"],
                    "ensemble_prob": round(prob, 4),
                    "edge": round((prob - r["yes_price"]) * 100, 1) if prob > 0 else 0,
                    "volume": r["volume"],
                })
            result.append({
                "event_slug": slug,
                "city": city["name"],
                "city_slug": city["slug"],
                "date": mdata["date"],
                "title": mdata["title"],
                "volume": mdata["volume"],
                "ranges": ranges_info,
                "mean_temp": forecast.mean_max if forecast else None,
                "std_temp": forecast.std_max if forecast else None,
                "confidence": round(forecast.confidence * 100, 1) if forecast else 0,
            })
        return result

    def get_stats(self) -> dict:
        """Estadísticas del detector para el dashboard."""
        total = len(self._signals_today)
        resolved = [s for s in self._signals_today if s.resolved]
        wins = [s for s in resolved if s.result == "win"]
        return {
            "active_markets": len(self._active_markets),
            "signals_today": total,
            "resolved": len(resolved),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(resolved) * 100, 1) if resolved else 0,
            "paper_pnl": round(sum(s.paper_pnl for s in resolved), 2),
            "cities_monitored": len(self._cities_to_scan()),
        }
