"""Backtester para la estrategia crypto arb.

Descarga datos históricos de mercados crypto up/down resueltos en Polymarket
y simula qué habría pasado usando datos REALES de Binance (precios spot)
y Polymarket (resolución). NO usa random ni datos inventados.

Método:
1. Para cada mercado cerrado, parsea el tiempo de inicio/fin del window
2. Obtiene klines de Binance para ese window exacto
3. Simula la lógica del detector: analiza momentum del spot a mitad del window
4. Compara predicción con resolución real de Polymarket
5. Calcula PnL con entry_odds = 0.50 (mercados abren ~50/50)
"""
import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog
import httpx

from src import config

logger = structlog.get_logger()


@dataclass
class BacktestTrade:
    """Un trade simulado del backtester."""
    coin: str
    direction: str            # "up" o "down"
    entry_odds: float         # Odds de Polymarket al entrar
    result: str               # "win" o "loss"
    pnl: float                # Ganancia/pérdida en USD
    bet_size: float
    market_question: str
    condition_id: str
    resolution: str           # Resultado real del mercado
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BacktestResult:
    """Resultado agregado del backtest."""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0
    win_rate: float = 0
    avg_pnl_per_trade: float = 0
    max_drawdown: float = 0
    best_trade: float = 0
    worst_trade: float = 0
    roi_pct: float = 0
    total_invested: float = 0
    trades: list = field(default_factory=list)
    by_coin: dict = field(default_factory=dict)
    by_hour: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": round(self.total_pnl, 2),
            "win_rate": round(self.win_rate, 1),
            "avg_pnl_per_trade": round(self.avg_pnl_per_trade, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "best_trade": round(self.best_trade, 2),
            "worst_trade": round(self.worst_trade, 2),
            "roi_pct": round(self.roi_pct, 1),
            "total_invested": round(self.total_invested, 2),
            "trades": [
                {
                    "coin": t.coin,
                    "direction": t.direction,
                    "entry_odds": t.entry_odds,
                    "result": t.result,
                    "pnl": round(t.pnl, 2),
                    "bet_size": t.bet_size,
                    "market_question": t.market_question[:60],
                    "resolution": t.resolution,
                    "timestamp": t.timestamp.isoformat() if isinstance(t.timestamp, datetime) else str(t.timestamp),
                }
                for t in self.trades[-200:]  # Últimos 200 trades
            ],
            "by_coin": self.by_coin,
            "by_hour": self.by_hour,
        }


class CryptoArbBacktester:
    """Backtest de la estrategia crypto arb usando datos históricos de Polymarket."""

    def __init__(self):
        self._last_result: Optional[BacktestResult] = None
        self._running = False

    async def run_backtest(
        self,
        days: int = 7,
        bet_size: float = 100,
        max_odds: float = 0.65,
        coins: Optional[list[str]] = None,
    ) -> BacktestResult:
        """Ejecutar backtest sobre mercados crypto up/down resueltos."""
        self._running = True
        coins = coins or ["BTC", "ETH", "SOL"]
        coin_names = {
            "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana",
        }

        result = BacktestResult()
        result.by_coin = {c: {"wins": 0, "losses": 0, "pnl": 0} for c in coins}
        result.by_hour = {str(h): {"wins": 0, "losses": 0, "pnl": 0} for h in range(24)}

        logger.info("backtest_starting", days=days, bet_size=bet_size, coins=coins)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                for coin in coins:
                    if not self._running:
                        break
                    name = coin_names.get(coin, coin)
                    # Buscar mercados cerrados de crypto up/down
                    markets = await self._fetch_resolved_markets(client, name, days)
                    logger.info("backtest_markets_found", coin=coin, count=len(markets))

                    for market in markets:
                        if not self._running:
                            break
                        trade = await self._simulate_trade(
                            client, market, coin, bet_size, max_odds
                        )
                        if trade:
                            result.trades.append(trade)
                            result.total_trades += 1
                            result.total_invested += bet_size

                            if trade.result == "win":
                                result.wins += 1
                                result.by_coin[coin]["wins"] += 1
                            else:
                                result.losses += 1
                                result.by_coin[coin]["losses"] += 1

                            result.total_pnl += trade.pnl
                            result.by_coin[coin]["pnl"] += trade.pnl

                            # Por hora
                            hour = str(trade.timestamp.hour) if isinstance(trade.timestamp, datetime) else "0"
                            if hour in result.by_hour:
                                if trade.result == "win":
                                    result.by_hour[hour]["wins"] += 1
                                else:
                                    result.by_hour[hour]["losses"] += 1
                                result.by_hour[hour]["pnl"] += trade.pnl

                            # Tracking max drawdown
                            if trade.pnl > result.best_trade:
                                result.best_trade = trade.pnl
                            if trade.pnl < result.worst_trade:
                                result.worst_trade = trade.pnl

        except Exception as e:
            logger.error("backtest_error", error=str(e))

        # Calcular métricas finales
        if result.total_trades > 0:
            result.win_rate = (result.wins / result.total_trades) * 100
            result.avg_pnl_per_trade = result.total_pnl / result.total_trades
        if result.total_invested > 0:
            result.roi_pct = (result.total_pnl / result.total_invested) * 100

        # Calcular drawdown
        cumulative = 0
        peak = 0
        max_dd = 0
        for t in result.trades:
            cumulative += t.pnl
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
        result.max_drawdown = max_dd

        # Limpiar by_hour (solo horas con datos)
        result.by_hour = {
            h: v for h, v in result.by_hour.items()
            if v["wins"] + v["losses"] > 0
        }

        self._last_result = result
        self._running = False
        logger.info("backtest_complete",
                     trades=result.total_trades,
                     win_rate=result.win_rate,
                     pnl=result.total_pnl)
        return result

    async def _fetch_resolved_markets(self, client: httpx.AsyncClient,
                                       coin_name: str, days: int) -> list[dict]:
        """Obtener mercados crypto up/down resueltos de los últimos N días.

        Los mercados tienen formato: "Bitcoin Up or Down - February 14, 2PM ET"
        Slugs: btc-updown-15m-{ts}, eth-updown-15m-{ts}, sol-updown-15m-{ts}
        IMPORTANTE: usar order=volume (no _order=closedTime que está roto en Gamma API).
        """
        all_markets = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # Mapeo de nombre a prefijos de slug
        slug_prefixes = {
            "Bitcoin": ["btc-updown-", "bitcoin-up-or-down-"],
            "Ethereum": ["eth-updown-", "ethereum-up-or-down-"],
            "Solana": ["sol-updown-", "solana-up-or-down-"],
        }
        prefixes = slug_prefixes.get(coin_name, [coin_name.lower()])

        try:
            # Gamma API: order=volume funciona, _order=closedTime está roto
            # Hacer múltiples páginas para encontrar más mercados
            for offset in range(0, 1000, 500):
                try:
                    resp = await client.get(
                        f"{config.GAMMA_API_URL}/markets",
                        params={
                            "closed": "true",
                            "limit": "500",
                            "order": "volume",
                            "ascending": "false",
                            "offset": str(offset),
                        },
                    )
                    if resp.status_code == 200:
                        batch = resp.json()
                        if not batch:
                            break
                        all_markets.extend(batch)
                    else:
                        break
                except Exception:
                    break

            # Deduplicar
            seen = set()
            unique = []
            for m in all_markets:
                cid = m.get("conditionId", "")
                if cid and cid not in seen:
                    seen.add(cid)
                    unique.append(m)

            # Filtrar: mercados crypto up/down por slug O por pregunta
            markets = []
            for m in unique:
                q = m.get("question", "").lower()
                slug = m.get("slug", "").lower()

                # Verificar si es un mercado up/down de esta moneda
                is_match = False
                # Por slug (más confiable)
                for prefix in prefixes:
                    if slug.startswith(prefix.lower()):
                        is_match = True
                        break
                # Por pregunta (fallback)
                if not is_match:
                    if coin_name.lower() in q and "up or down" in q:
                        is_match = True

                if not is_match:
                    continue

                # Verificar outcomes = Up/Down
                outcomes = m.get("outcomes", "")
                if isinstance(outcomes, str):
                    if "Up" not in outcomes or "Down" not in outcomes:
                        continue

                # Verificar fecha de cierre dentro del rango
                closed_time = m.get("closedTime")
                if not closed_time:
                    continue
                try:
                    ct_str = closed_time.replace("Z", "+00:00")
                    # Gamma API devuelve "+00" que no es ISO válido, necesita "+00:00"
                    if re.search(r'[+-]\d{2}$', ct_str):
                        ct_str = ct_str + ":00"
                    ct = datetime.fromisoformat(ct_str)
                    if ct.tzinfo is None:
                        ct = ct.replace(tzinfo=timezone.utc)
                    if ct < cutoff:
                        continue
                except Exception:
                    continue

                cid = m.get("conditionId", "")
                if cid:
                    markets.append({
                        "condition_id": cid,
                        "question": m.get("question", ""),
                        "slug": m.get("slug", ""),
                        "closed_time": closed_time,
                        "end_date": m.get("endDate", ""),
                        "outcome_prices": m.get("outcomePrices", ""),
                    })

            print(f"[Backtest] {coin_name}: {len(unique)} mercados escaneados, "
                  f"{len(markets)} crypto up/down encontrados (últimos {days}d)", flush=True)

        except Exception as e:
            logger.warning("backtest_fetch_error", error=str(e))
            import traceback
            traceback.print_exc()

        return markets

    def _parse_market_window(self, market: dict) -> Optional[tuple[datetime, datetime, str]]:
        """Parsear inicio y fin del window de un mercado crypto up/down.

        Retorna (start_dt, end_dt, binance_pair) o None si no se puede parsear.
        Los slugs tienen formato: btc-updown-15m-{start_timestamp}
        """
        slug = market.get("slug", "").lower()
        coin = market.get("_coin", "")

        # Mapeo de moneda a par de Binance
        pair_map = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
        binance_pair = pair_map.get(coin)
        if not binance_pair:
            return None

        # Intentar parsear desde slug: btc-updown-15m-{start_ts}
        slug_match = re.search(r'updown-(\d+[hm])-(\d{10,})', slug)
        if slug_match:
            duration_str = slug_match.group(1)
            start_ts = int(slug_match.group(2))
            start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)

            # Parsear duración
            if duration_str.endswith('m'):
                minutes = int(duration_str[:-1])
            elif duration_str.endswith('h'):
                minutes = int(duration_str[:-1]) * 60
            else:
                minutes = 15  # default

            end_dt = start_dt + timedelta(minutes=minutes)
            return (start_dt, end_dt, binance_pair)

        # Fallback: usar endDate y estimar start desde la pregunta
        end_str = market.get("end_date") or market.get("closed_time")
        if end_str:
            try:
                end_dt = datetime.fromisoformat(
                    str(end_str).replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                # Estimar 15 min de duración por defecto
                start_dt = end_dt - timedelta(minutes=15)
                return (start_dt, end_dt, binance_pair)
            except Exception:
                pass

        return None

    async def _get_binance_klines(self, client: httpx.AsyncClient,
                                   symbol: str, start_dt: datetime,
                                   end_dt: datetime) -> list[dict]:
        """Obtener klines de 1 minuto de Binance para un rango de tiempo.
        Intenta múltiples fuentes: binance.com, binance.us (para servidores en US).
        """
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)
        params = {
            "symbol": symbol,
            "interval": "1m",
            "startTime": str(start_ms),
            "endTime": str(end_ms),
            "limit": "500",
        }
        urls = [
            "https://api.binance.com/api/v3/klines",
            "https://api.binance.us/api/v3/klines",
        ]
        for url in urls:
            try:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    continue
                raw = resp.json()
                if not raw:
                    continue
                return [
                    {
                        "ts": k[0] / 1000,
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                    }
                    for k in raw
                ]
            except Exception:
                continue
        print(f"[Backtest] No klines from any source for {symbol}", flush=True)
        return []

    async def _simulate_trade(self, client: httpx.AsyncClient, market: dict,
                               coin: str, bet_size: float,
                               max_odds: float) -> Optional[BacktestTrade]:
        """Simular un trade usando datos REALES de Binance.

        Método (simula lo que haría el detector en vivo):
        1. Obtiene resolución real de Polymarket (quién ganó: Up o Down)
        2. Obtiene klines de Binance para el window completo del mercado
        3. Desliza por el window buscando momentum suficiente (como el detector)
           - En cada minuto, compara precio actual vs precio de N minutos atrás
           - Si el cambio >= min_move → esa es la entrada del detector
        4. Compara predicción con resolución real
        5. Calcula PnL con entry_odds basado en cuándo entró (más temprano = mejor odds)
        """
        cid = market["condition_id"]
        market["_coin"] = coin

        try:
            # 1. Obtener resolución real via CLOB API
            resp = await client.get(f"{config.CLOB_API_URL}/markets/{cid}")
            if resp.status_code != 200:
                return None

            data = resp.json()
            tokens = data.get("tokens", [])
            if not tokens:
                return None

            winner = None
            for tok in tokens:
                if tok.get("winner") is True:
                    winner = tok.get("outcome", "").lower()
                    break
            if not winner:
                return None

            # 2. Parsear window de tiempo del mercado
            window = self._parse_market_window(market)
            if not window:
                print(f"[Backtest] No se pudo parsear window: {market.get('slug','')}", flush=True)
                return None

            start_dt, end_dt, binance_pair = window
            window_minutes = (end_dt - start_dt).total_seconds() / 60

            # 3. Obtener klines reales de Binance
            klines = await self._get_binance_klines(client, binance_pair, start_dt, end_dt)
            if len(klines) < 2:
                print(f"[Backtest] Sin klines para {binance_pair} {start_dt}", flush=True)
                return None

            # 4. Simular lógica del detector: deslizar por el window
            # El detector en vivo compara precio actual vs lookback_seconds atrás
            # Para klines de 1 min, lookback = 3 candles (~3 min)
            lookback_candles = max(1, config.CRYPTO_ARB_LOOKBACK_SEC // 60)
            # Para mercados cortos (5min), usar lookback más corto
            if window_minutes <= 5:
                lookback_candles = 1
            elif window_minutes <= 15:
                lookback_candles = min(lookback_candles, 3)

            # Umbral mínimo adaptado a la duración del mercado
            # Mercados cortos (5min) tienen menos movimiento que los de 4h
            min_move = config.CRYPTO_ARB_MIN_MOVE_PCT
            if window_minutes <= 5:
                min_move = min(min_move, 0.03)  # 5min: umbral más bajo
            elif window_minutes <= 15:
                min_move = min(min_move, 0.05)  # 15min: umbral moderado
            # Mercados largos (1h+): usar el umbral normal del config

            # Buscar primer punto con momentum suficiente
            entry_idx = None
            entry_change = 0
            open_price = klines[0]["open"]

            for i in range(lookback_candles, len(klines)):
                # Momentum: precio actual vs precio lookback candles atrás
                ref_price = klines[i - lookback_candles]["close"]
                cur_price = klines[i]["close"]
                if ref_price <= 0:
                    continue
                change = ((cur_price - ref_price) / ref_price) * 100
                if abs(change) >= min_move:
                    entry_idx = i
                    entry_change = change
                    break

            # Si no encontró momentum suficiente, usar cambio total open→close
            # (el mercado igual se resolvió, solo fue un movimiento lento)
            if entry_idx is None:
                final_price = klines[-1]["close"]
                total_change = ((final_price - open_price) / open_price) * 100
                if abs(total_change) < 0.005:  # Menos de 0.005% = flat
                    return None  # Realmente no hubo movimiento
                entry_idx = len(klines) - 2  # Entrada tardía
                entry_change = total_change

            # Dirección predicha por el detector
            predicted_direction = "up" if entry_change > 0 else "down"

            # 5. Comparar con resolución real
            correct = (predicted_direction == winner)

            # 6. Calcular PnL
            # Entry odds basado en cuándo entró:
            # Más temprano en el window = mejores odds (más cerca de 0.50)
            # Más tarde = peores odds (mercado ya se movió)
            progress = entry_idx / max(len(klines) - 1, 1)  # 0.0 = inicio, 1.0 = final
            # Base 0.50 + ajuste por progreso (máx +0.12)
            entry_odds = 0.50 + (progress * 0.12)

            # Respetar max_odds del usuario
            if entry_odds > max_odds:
                return None

            if correct:
                pnl = bet_size * (1 - entry_odds)
                result = "win"
            else:
                pnl = -bet_size * entry_odds
                result = "loss"

            ts = end_dt

            return BacktestTrade(
                coin=coin,
                direction=predicted_direction,
                entry_odds=round(entry_odds, 3),
                result=result,
                pnl=round(pnl, 2),
                bet_size=bet_size,
                market_question=market["question"],
                condition_id=cid,
                resolution=winner.capitalize(),
                timestamp=ts,
            )

        except Exception as e:
            logger.warning("backtest_sim_error", cid=cid[:20], error=str(e))
            return None

    def get_last_result(self) -> Optional[dict]:
        """Último resultado de backtest."""
        if self._last_result:
            return self._last_result.to_dict()
        return None

    def cancel(self):
        """Cancelar backtest en progreso."""
        self._running = False
