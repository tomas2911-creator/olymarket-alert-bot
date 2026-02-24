"""Entry point principal — FastAPI + background polling loop."""
import asyncio
import sys
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

print("=== POLYMARKET ALERT BOT v3.0 INICIANDO ===", flush=True)

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import httpx

from src import config
from src.api.polymarket import PolymarketClient
from src.storage.database import Database
from src.detection.analyzer import AnomalyAnalyzer
from src.alerts.telegram import TelegramNotifier
from src.api.routes import router
from src.api.polygonscan import get_wallet_onchain_info
from src.crypto_arb.binance_feed import BinanceFeed
from src.crypto_arb.detector import CryptoArbDetector
from src.crypto_arb.early_detector import EarlyEntryDetector
from src.crypto_arb.backtester import CryptoArbBacktester
from src.crypto_arb.autotrader import AutoTrader
from src.alerts.alert_autotrader import AlertAutoTrader
from src.strategies.market_maker import MarketMakerBot
from src.strategies.spike_detector import SpikeDetector
from src.strategies.event_driven import EventDrivenBot
from src.strategies.cross_platform import CrossPlatformArb
from src.strategies.complement_arb import ComplementArbScanner
from src.infra.rate_limiter import get_limiter
from src.infra.queue_system import SignalQueue
from src.infra.websocket_client import PolymarketWebSocket
from src.infra.bankroll import BankrollTracker
from src.infra.news_catalyst import NewsCatalyst
from src.infra.ml_scoring import MLScorer
from src.detection.smart_score import SmartScoreCalculator
from src.detection.insider_detector import InsiderDetector
from src.detection.whale_scanner import WhaleScanner
from src.detection.news_fetcher import NewsFetcher
from src.detection.sentiment_analyzer import SentimentAnalyzer
from src.trading.copy_engine import CopyTradingEngine
from src.ai.market_agent import MarketAgent
from src.weather_arb.weather_feed import WeatherFeed
from src.weather_arb.detector import WeatherArbDetector
from src.weather_arb.autotrader import WeatherAutoTrader
from src.weather_arb.backtester import WeatherPaperTrader
from src.weather_arb.multi_feed import MultiWeatherFeed
from src.weather_arb.early_weather import EarlyWeatherDetector
from src.weather_arb.metar_feed import MetarFeed
from src.weather_arb.wu_feed import WundergroundFeed
from src.crypto_arb.paper_trader import MakerPaperTrader

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger()


# ── Bot Class ────────────────────────────────────────────────────────

class PolymarketAlertBot:
    """Orquesta polling, detección, alertas y resolución."""

    def __init__(self):
        self.db = Database()
        self.analyzer = AnomalyAnalyzer()
        self.notifier = TelegramNotifier()
        self.start_time = datetime.now()
        self.trades_processed = 0
        self.alerts_sent = 0
        self._running = False
        # Debug counters
        self._debug = {"too_small": 0, "scored": 0, "low_score": 0, "excluded_cat": 0, "cooldown": 0, "alerted": 0, "last_scores": []}
        self._watchlist: set[str] = set()
        self._excluded_categories: set[str] = set()
        self._last_markets = []  # Cache de mercados para spike detector
        self._pipeline_stats = {}  # Stats del pipeline de trades para dashboard
        # Crypto Arb (inicializado solo si feature está habilitada)
        self.binance_feed = None
        self.crypto_detector = None
        self.early_detector = None
        self.backtester = CryptoArbBacktester()
        self.autotrader = None
        self.alert_autotrader = None
        # v8.0: Nuevos módulos
        self.market_maker = None
        self.crypto_market_maker = None  # Market Maker Bilateral
        self.spike_detector = None
        self.event_driven = None
        self.cross_platform = None
        # v10.0: Complement Arb Scanner
        self.complement_arb = None
        self.rate_limiter = get_limiter()
        self.signal_queue = SignalQueue()
        self.ws_client = None
        self.bankroll = None
        self.news_catalyst = NewsCatalyst()
        self.ml_scorer = None
        # v11: Nuevos módulos
        self.smart_score_calc = SmartScoreCalculator()
        self.insider_detector = InsiderDetector()
        self.news_fetcher = None
        self.sentiment_analyzer = SentimentAnalyzer()
        self.copy_engine = None
        self.market_agent = None
        self.whale_scanner = None
        # Weather Arb
        self.weather_feed = None
        self.weather_detector = None
        self.weather_autotrader = None
        self.weather_paper = None
        self.weather_multi_feed = None
        self.weather_early_detector = None
        # Paper Trading Maker
        self.paper_trader = None

    async def start(self):
        """Inicializar DB y marcar como running."""
        try:
            await self.db.connect()
            print("DB PostgreSQL conectada OK", flush=True)
        except Exception as e:
            print(f"ERROR conectando DB: {e}", flush=True)
            raise
        self._running = True
        # Restaurar TODAS las configs desde DB (persisten entre reinicios)
        try:
            saved_cfg = await self.db.get_config()
            config.restore_from_db(saved_cfg)
            exc_str = saved_cfg.get("excluded_categories", "")
            self._excluded_categories = {c.strip().lower() for c in exc_str.split(",") if c.strip()}
            restored = len([k for k in saved_cfg if k != "excluded_categories"])
            if restored:
                print(f"Config restaurada desde DB: {restored} valores", flush=True)
        except Exception as e:
            print(f"Error restaurando config: {e}", flush=True)
            self._excluded_categories = set()
        # Telegram startup en background para no bloquear healthcheck
        asyncio.create_task(self._send_startup_safe())
        # Iniciar crypto arb si está habilitado
        if config.FEATURE_CRYPTO_ARB:
            await self._start_crypto_arb()
        # Iniciar alert autotrader (independiente de crypto arb)
        try:
            self.alert_autotrader = AlertAutoTrader(self.db)
            await self.alert_autotrader.initialize()
        except Exception as e:
            print(f"Error iniciando Alert AutoTrader: {e}", flush=True)
            self.alert_autotrader = None
        # v10.0: Complement Arb Scanner
        try:
            self.complement_arb = ComplementArbScanner(self.db)
            await self.complement_arb.initialize()
            if config.FEATURE_COMPLEMENT_ARB:
                asyncio.create_task(self._run_complement_arb())
        except Exception as e:
            print(f"Error iniciando Complement Arb: {e}", flush=True)
            self.complement_arb = None
        # v8.0: Iniciar nuevos módulos
        await self._start_v8_modules()
        # v11: Iniciar módulos nuevos
        self.news_fetcher = NewsFetcher(self.db)
        self.copy_engine = CopyTradingEngine(self.db)
        self.market_agent = MarketAgent(self.db)
        # Whale Scanner híbrido (agregación de fills + monitoreo de actividad)
        self.whale_scanner = WhaleScanner(self.db)
        if config.WHALE_TRACKER_ENABLED:
            asyncio.create_task(self.whale_scanner.run_activity_loop())
        print(f"v11 módulos iniciados: News={True} CopyTrading={True} AI={self.market_agent.enabled} WhaleScanner={config.WHALE_TRACKER_ENABLED}", flush=True)
        # Weather Arb
        if config.FEATURE_WEATHER_ARB:
            await self._start_weather_arb()

    async def _send_startup_safe(self):
        try:
            await self.notifier.send_startup_message()
            print("Mensaje startup enviado", flush=True)
        except Exception as e:
            print(f"Error enviando startup a Telegram: {e}", flush=True)

    async def _start_v8_modules(self):
        """Inicializar módulos v8.0."""
        try:
            # Bankroll tracker
            self.bankroll = BankrollTracker(self.db)
            # ML Scorer
            self.ml_scorer = MLScorer(self.db)
            if config.FEATURE_ML_SCORING:
                await self.ml_scorer.train()
            # Market Maker
            self.market_maker = MarketMakerBot(self.db)
            if config.FEATURE_MARKET_MAKING:
                await self.market_maker.start()
            # Spike Detector
            self.spike_detector = SpikeDetector(self.db)
            # Event Driven
            self.event_driven = EventDrivenBot(self.db)
            # Cross Platform Arb
            self.cross_platform = CrossPlatformArb(self.db)
            # WebSocket — conectar con callback de trades para captura en tiempo real
            if config.FEATURE_WEBSOCKET:
                self.ws_client = PolymarketWebSocket(on_trade=self._on_ws_trade)
                await self.ws_client.start()
                # Auto-suscribir a mercados activos
                if self._last_markets:
                    market_ids = [m.condition_id for m in self._last_markets[:50]]
                    await self.ws_client.subscribe(market_ids)
                    print(f"WebSocket: suscrito a {len(market_ids)} mercados", flush=True)
            # Queue System
            if config.FEATURE_QUEUE:
                await self.signal_queue.start_workers(self._process_queued_signal)
            v8_count = sum(1 for f in [
                config.FEATURE_CORRELATION_FILTER, config.FEATURE_MULTI_TIMEFRAME,
                config.FEATURE_VWAP, config.FEATURE_ORDERBOOK_CRYPTO,
                config.FEATURE_HEDGING, config.FEATURE_NEWS_CATALYST,
                config.FEATURE_ML_SCORING, config.FEATURE_HEATMAP,
                config.FEATURE_TRADE_JOURNAL, config.FEATURE_PUSH_NOTIFICATIONS,
                config.FEATURE_WEBSOCKET, config.FEATURE_QUEUE,
                config.FEATURE_RATE_LIMITING, config.FEATURE_BANKROLL,
                config.FEATURE_MARKET_MAKING, config.FEATURE_EVENT_DRIVEN,
                config.FEATURE_SPIKE_DETECTION, config.FEATURE_CROSS_PLATFORM,
            ] if f)
            print(f"v8.0 módulos inicializados ({v8_count} activos de 18)", flush=True)
        except Exception as e:
            print(f"Error iniciando módulos v8.0: {e}", flush=True)

    async def _process_queued_signal(self, signal: dict):
        """Procesar una señal de la cola."""
        stype = signal.get("type", "")
        if stype == "alert" and self.alert_autotrader:
            await self.alert_autotrader.process_signal(signal)
        elif stype == "crypto" and self.autotrader:
            await self.autotrader.process_signal(signal)

    async def _start_crypto_arb(self):
        """Inicializar módulo crypto arb."""
        try:
            pairs = [c["binance_pair"] for c in config.CRYPTO_ARB_COINS]
            self.binance_feed = BinanceFeed(pairs=pairs)
            self.crypto_detector = CryptoArbDetector(self.binance_feed)
            # Inicializar early entry detector
            self.early_detector = EarlyEntryDetector(self.binance_feed)
            # Inicializar autotrader
            self.autotrader = AutoTrader(self.db)
            await self.autotrader.initialize()
            # Configurar early detector desde DB
            await self._configure_early_detector()
            # Inicializar paper trader maker
            self.paper_trader = MakerPaperTrader()
            # Inicializar market maker bilateral (usa detector + feed)
            from src.strategies.market_maker import CryptoMarketMaker
            self.crypto_market_maker = CryptoMarketMaker(
                self.db, binance_feed=self.binance_feed, detector=self.crypto_detector
            )
            await self.crypto_market_maker.initialize()
            # Cargar config paper trader desde DB
            try:
                pt_raw = await self.db.get_config_bulk([
                    "paper_trading_enabled", "paper_bet_size",
                    "paper_spread_offset", "paper_initial_capital", "paper_mode",
                ], user_id=1)
                pt_cfg = {}
                if pt_raw.get("paper_trading_enabled") == "true":
                    pt_cfg["enabled"] = True
                if pt_raw.get("paper_bet_size"):
                    pt_cfg["bet_size"] = float(pt_raw["paper_bet_size"])
                if pt_raw.get("paper_spread_offset"):
                    pt_cfg["spread_offset"] = float(pt_raw["paper_spread_offset"])
                if pt_raw.get("paper_initial_capital"):
                    pt_cfg["initial_capital"] = float(pt_raw["paper_initial_capital"])
                if pt_raw.get("paper_mode"):
                    pt_cfg["mode"] = pt_raw["paper_mode"]
                if pt_cfg:
                    self.paper_trader.configure(pt_cfg)
            except Exception as e:
                print(f"[PaperTrader] Error cargando config desde DB: {e}", flush=True)
            # Lanzar feed, detector, early detector y autotrader como tasks independientes
            asyncio.create_task(self._run_binance_feed())
            asyncio.create_task(self._run_crypto_detector())
            asyncio.create_task(self._run_early_detector())
            asyncio.create_task(self._run_crypto_autotrader())
            asyncio.create_task(self._run_market_maker())
            early_status = "ON" if self.early_detector.enabled else "OFF"
            print(f"Crypto Arb iniciado: {len(pairs)} pares, modo={config.CRYPTO_ARB_MODE}, early_entry={early_status}", flush=True)
        except Exception as e:
            print(f"Error iniciando Crypto Arb: {e}", flush=True)

    async def _run_binance_feed(self):
        """Wrapper para Binance feed con reconexión."""
        while self._running and self.binance_feed:
            try:
                await self.binance_feed.start()
            except Exception as e:
                print(f"Binance feed error, reintentando en 10s: {e}", flush=True)
                await asyncio.sleep(10)

    async def _run_crypto_detector(self):
        """Wrapper para detector crypto con manejo de señales."""
        await asyncio.sleep(5)
        try:
            await self.crypto_detector.start()
        except Exception as e:
            print(f"Crypto detector error: {e}", flush=True)

    async def _run_early_detector(self):
        """Wrapper para early entry detector."""
        await asyncio.sleep(5)
        try:
            await self.early_detector.start()
        except Exception as e:
            print(f"Early detector error: {e}", flush=True)

    async def _configure_early_detector(self):
        """Configurar early detector desde DB."""
        if not self.early_detector:
            return
        try:
            raw = await self.db.get_config_bulk([
                "at_early_entry_enabled", "at_early_entry_pre_monitor",
                "at_early_entry_window", "at_early_entry_min_momentum",
                "at_early_entry_min_momentum_5m", "at_early_entry_min_momentum_15m",
                "at_early_entry_bet_size",
            ], user_id=1)
            self.early_detector.configure({
                "early_entry_enabled": raw.get("at_early_entry_enabled") == "true",
                "early_entry_pre_monitor": int(raw.get("at_early_entry_pre_monitor", config.EARLY_ENTRY_PRE_MONITOR_SEC)),
                "early_entry_window": int(raw.get("at_early_entry_window", config.EARLY_ENTRY_WINDOW_SEC)),
                "early_entry_min_momentum": float(raw.get("at_early_entry_min_momentum", config.EARLY_ENTRY_MIN_MOMENTUM_PCT)),
                "early_entry_min_momentum_5m": float(raw.get("at_early_entry_min_momentum_5m",
                    raw.get("at_early_entry_min_momentum", config.EARLY_ENTRY_MIN_MOMENTUM_PCT))),
                "early_entry_min_momentum_15m": float(raw.get("at_early_entry_min_momentum_15m", 0.05)),
            })
        except Exception as e:
            print(f"[EarlyEntry] Error cargando config: {e}", flush=True)
            self.early_detector.configure({
                "early_entry_enabled": config.FEATURE_EARLY_ENTRY,
                "early_entry_pre_monitor": config.EARLY_ENTRY_PRE_MONITOR_SEC,
                "early_entry_window": config.EARLY_ENTRY_WINDOW_SEC,
                "early_entry_min_momentum": config.EARLY_ENTRY_MIN_MOMENTUM_PCT,
            })

    async def _run_market_maker(self):
        """Loop dedicado para market maker bilateral — tick cada 5s."""
        await asyncio.sleep(12)  # Esperar que detector tenga mercados
        while self._running and self.crypto_market_maker:
            try:
                await self.crypto_market_maker.tick()
            except Exception as e:
                print(f"[MarketMaker] Error en loop: {e}", flush=True)
            await asyncio.sleep(5)

    async def _configure_sniper_detector(self):
        """Pass sniper config from autotrader to detector."""
        if self.crypto_detector and self.autotrader and self.autotrader._config:
            self.crypto_detector.configure_sniper(self.autotrader._config)

    async def _run_crypto_autotrader(self):
        """Loop rápido: evaluar señales crypto cada 5s, independiente del polling.
        Combina señales de score strategy + early entry strategy.
        """
        await asyncio.sleep(8)
        # Pass sniper config from autotrader to detector on startup
        await self._configure_sniper_detector()
        while self._running and self.autotrader and self.crypto_detector:
            try:
                # Señales del detector score (strategy="score")
                signals = self.crypto_detector.get_recent_signals(200)
                # Señales del early entry detector (strategy="early_entry")
                if self.early_detector:
                    early_signals = self.early_detector.get_recent_signals(100)
                    if early_signals:
                        # Merge evitando duplicados por (condition_id + strategy)
                        existing_keys = {(s["condition_id"], s.get("strategy", "score")) for s in signals}
                        for es in early_signals:
                            key = (es["condition_id"], es.get("strategy", "early_entry"))
                            if key not in existing_keys:
                                signals.append(es)
                                existing_keys.add(key)
                if signals:
                    active = [s for s in signals if s.get("time_remaining_sec", 0) > 0]
                    if active:
                        await self.autotrader.process_signals(active)
                        # Paper trader: procesar las mismas señales
                        if self.paper_trader:
                            await self.paper_trader.process_signals(active)
                # Risk management cada ciclo (5s) — necesario para TP en mercados cortos
                await self.autotrader.check_risk_management()
                await self.autotrader.resolve_trades()
                # Maker orders: gestionar órdenes abiertas
                await self.autotrader.manage_maker_orders()
                # Paper trader: gestionar órdenes simuladas
                if self.paper_trader:
                    await self.paper_trader.manage_orders()
            except Exception as e:
                print(f"[CryptoAutotrader] Error en loop rápido: {e}", flush=True)
            await asyncio.sleep(5)

    # ── Weather Arb ──────────────────────────────────────────────────

    async def _start_weather_arb(self):
        """Inicializar módulo weather arb.

        Feed + Detector + Paper Trading siempre corren (no necesitan wallet).
        AutoTrader solo ejecuta trades reales si tiene wallet configurada.
        Multi-Source Feed y Early Detector son opcionales.
        """
        try:
            cities = config.WEATHER_ARB_CITIES  # None = todas
            self.weather_feed = WeatherFeed(
                cities=cities,
                refresh_interval=config.WEATHER_ARB_FORECAST_REFRESH,
            )
            # Multi-source feed (Weather.gov, WeatherAPI, Visual Crossing)
            self.weather_multi_feed = MultiWeatherFeed()
            self.weather_multi_feed.configure({
                "multi_source_enabled": config.WEATHER_MULTI_SOURCE_ENABLED,
                "multi_source_refresh_sec": config.WEATHER_MULTI_SOURCE_REFRESH,
                "weatherapi_key": config.WEATHERAPI_KEY,
                "visual_crossing_key": config.VISUAL_CROSSING_KEY,
            })
            # METAR feed (observaciones en tiempo real de aeropuertos)
            self.metar_feed = MetarFeed(
                cities=cities,
                refresh_interval=config.WEATHER_METAR_REFRESH,
            )
            self.metar_feed._enabled = config.WEATHER_METAR_ENABLED
            # WU feed (Weather Underground — fuente de resolución)
            self.wu_feed = WundergroundFeed(
                api_key=config.WU_API_KEY,
                refresh_sec=config.WEATHER_WU_REFRESH_SEC,
                cities=cities,
            )
            self.wu_feed._enabled = config.WEATHER_WU_ENABLED
            # Detector con multi-source + METAR + WU
            self.weather_detector = WeatherArbDetector(
                self.weather_feed,
                multi_feed=self.weather_multi_feed,
                metar_feed=self.metar_feed,
                wu_feed=self.wu_feed,
            )
            self.weather_detector.configure({
                "min_edge": config.WEATHER_ARB_MIN_EDGE,
                "min_confidence": config.WEATHER_ARB_MIN_CONFIDENCE,
                "max_poly_odds": config.WEATHER_ARB_MAX_POLY_ODDS,
                "scan_interval": config.WEATHER_ARB_SCAN_INTERVAL,
                "enabled_cities": cities,
                "conviction_enabled": config.WEATHER_CONVICTION_ENABLED,
                "elimination_enabled": config.WEATHER_ELIMINATION_ENABLED,
                "elimination_min_profit": config.WEATHER_ELIMINATION_MIN_PROFIT,
                "elimination_max_bet": config.WEATHER_ELIMINATION_MAX_BET,
                "elimination_require_zero": config.WEATHER_ELIMINATION_REQUIRE_ZERO,
                "observation_enabled": config.WEATHER_OBSERVATION_ENABLED,
                "observation_min_hour": config.WEATHER_OBSERVATION_MIN_HOUR,
                "observation_high_confidence_hour": config.WEATHER_OBSERVATION_HIGH_CONF_HOUR,
                "observation_max_poly_odds": config.WEATHER_OBSERVATION_MAX_POLY_ODDS,
                "wu_enabled": config.WEATHER_WU_ENABLED,
                "wu_min_hour": config.WEATHER_WU_MIN_HOUR,
                "wu_high_confidence_hour": config.WEATHER_WU_HIGH_CONF_HOUR,
                "wu_min_edge": config.WEATHER_WU_MIN_EDGE,
                "wu_max_poly_odds": config.WEATHER_WU_MAX_POLY_ODDS,
            })
            self.weather_paper = WeatherPaperTrader(bet_size=config.WEATHER_ARB_PAPER_BET, db=self.db)
            await self.weather_paper.load_from_db()
            self.weather_autotrader = WeatherAutoTrader(self.db)
            await self.weather_autotrader.initialize()
            # Early weather detector
            self.weather_early_detector = EarlyWeatherDetector(self.weather_feed)
            self.weather_early_detector.configure({
                "early_enabled": config.WEATHER_EARLY_ENABLED,
                "early_scan_interval": config.WEATHER_EARLY_SCAN_INTERVAL,
                "early_min_edge": config.WEATHER_EARLY_MIN_EDGE,
                "early_min_confidence": config.WEATHER_EARLY_MIN_CONFIDENCE,
                "early_entry_window_sec": config.WEATHER_EARLY_ENTRY_WINDOW,
                "enabled_cities": cities,
            })
            # Lanzar loops de background (siempre — cada loop tiene if _enabled interno)
            asyncio.create_task(self._run_weather_feed())
            asyncio.create_task(self._run_weather_detector())
            asyncio.create_task(self._run_weather_signal_loop())
            asyncio.create_task(self._run_weather_multi_feed())
            asyncio.create_task(self._run_weather_early_detector())
            asyncio.create_task(self._run_metar_feed())
            asyncio.create_task(self._run_wu_feed())
            at_status = "ON" if self.weather_autotrader._enabled and self.weather_autotrader._client else "OFF (solo señales + paper)"
            multi_status = "ON" if config.WEATHER_MULTI_SOURCE_ENABLED else "OFF"
            early_status = "ON" if config.WEATHER_EARLY_ENABLED else "OFF"
            elim_status = "ON" if config.WEATHER_ELIMINATION_ENABLED else "OFF"
            metar_status = "ON" if config.WEATHER_METAR_ENABLED else "OFF"
            obs_status = "ON" if config.WEATHER_OBSERVATION_ENABLED else "OFF"
            wu_status = "ON" if config.WEATHER_WU_ENABLED else "OFF"
            print(f"Weather Arb iniciado: cities={cities or 'ALL'} "
                  f"edge>={config.WEATHER_ARB_MIN_EDGE}% "
                  f"conf>={config.WEATHER_ARB_MIN_CONFIDENCE}% "
                  f"autotrader={at_status} multi_source={multi_status} "
                  f"early={early_status} elimination={elim_status} "
                  f"metar={metar_status} observation={obs_status} "
                  f"wunderground={wu_status}", flush=True)
        except Exception as e:
            print(f"Error iniciando Weather Arb: {e}", flush=True)
            import traceback
            traceback.print_exc()

    async def _run_weather_feed(self):
        """Wrapper para weather feed con reconexión."""
        while self._running and self.weather_feed:
            try:
                await self.weather_feed.start()
            except Exception as e:
                print(f"[WeatherFeed] Error, reintentando en 30s: {e}", flush=True)
            if self._running:
                await asyncio.sleep(30)

    async def _run_weather_detector(self):
        """Wrapper para detector weather."""
        await asyncio.sleep(10)  # Esperar que el feed tenga datos
        while self._running and self.weather_detector:
            try:
                await self.weather_detector.start()
            except Exception as e:
                print(f"[WeatherDetector] Error: {e}", flush=True)
            if self._running:
                await asyncio.sleep(30)

    async def _run_weather_multi_feed(self):
        """Loop para multi-source weather feed."""
        await asyncio.sleep(20)
        while self._running and self.weather_multi_feed:
            try:
                await self.weather_multi_feed.start()
            except Exception as e:
                print(f"[MultiFeed] Error: {e}", flush=True)
            if self._running:
                await asyncio.sleep(60)

    async def _run_weather_early_detector(self):
        """Loop para early weather detector."""
        await asyncio.sleep(15)
        while self._running and self.weather_early_detector:
            try:
                await self.weather_early_detector.start()
            except Exception as e:
                print(f"[EarlyWeather] Error: {e}", flush=True)
            if self._running:
                await asyncio.sleep(30)

    async def _run_metar_feed(self):
        """Loop para METAR observation feed (aviationweather.gov)."""
        await asyncio.sleep(5)  # Iniciar rápido para tener observaciones pronto
        while self._running and hasattr(self, 'metar_feed') and self.metar_feed:
            try:
                await self.metar_feed.start()
            except Exception as e:
                print(f"[MetarFeed] Error: {e}", flush=True)
            if self._running:
                await asyncio.sleep(30)

    async def _run_wu_feed(self):
        """Loop para Weather Underground feed (fuente de resolución)."""
        await asyncio.sleep(8)  # Esperar un poco después de METAR
        while self._running and hasattr(self, 'wu_feed') and self.wu_feed:
            try:
                await self.wu_feed.start()
            except Exception as e:
                print(f"[WU Feed] Error: {e}", flush=True)
            if self._running:
                await asyncio.sleep(30)

    async def _run_weather_signal_loop(self):
        """Loop principal weather: señales + paper trading (siempre) + autotrading (si wallet).

        Este loop NO requiere wallet ni autotrader habilitado.
        Las señales y el paper trading funcionan independientemente.
        Risk management activo cada 30s para TP/SL/trailing.
        """
        await asyncio.sleep(15)  # Esperar que feed y detector tengan datos
        while self._running and self.weather_detector:
            try:
                # Señales del detector principal (conviction + observation + elimination)
                signals = self.weather_detector.get_recent_signals(50)

                # Señales del early detector (si activo)
                if self.weather_early_detector and self.weather_early_detector.enabled:
                    early_signals = self.weather_early_detector.get_recent_signals(20)
                    if early_signals:
                        # Combinar: early signals primero (prioridad)
                        existing_cids = {s.get("condition_id") for s in signals}
                        for es in early_signals:
                            if es.get("condition_id") not in existing_cids:
                                signals.append(es)

                if signals:
                    # Paper trading: registrar TODAS las señales (no necesita wallet)
                    if self.weather_paper:
                        for s in signals:
                            self.weather_paper.record_signal(s)
                    # Autotrading real: solo si está habilitado y tiene wallet
                    if self.weather_autotrader:
                        await self.weather_autotrader.process_signals(signals)

                # Resolver paper trades (no necesita wallet)
                if self.weather_paper:
                    await self.weather_paper.resolve_pending()

                # Risk management activo: TP/SL/Trailing (cada ciclo, ~30s)
                if self.weather_autotrader:
                    try:
                        await self.weather_autotrader.check_risk_management()
                    except Exception as e:
                        print(f"[WeatherRisk] Error: {e}", flush=True)

                # Resolver trades reales cerrados
                if self.weather_autotrader:
                    try:
                        await self.weather_autotrader.resolve_trades()
                    except Exception:
                        pass
            except Exception as e:
                print(f"[WeatherSignalLoop] Error: {e}", flush=True)
            if self._running:
                await asyncio.sleep(30)

    async def stop(self):
        self._running = False
        if self.ws_client:
            await self.ws_client.stop()
        if self.binance_feed:
            await self.binance_feed.stop()
        if self.crypto_detector:
            await self.crypto_detector.stop()
        if self.early_detector:
            await self.early_detector.stop()
        if self.weather_feed:
            await self.weather_feed.stop()
        if self.weather_detector:
            await self.weather_detector.stop()
        if hasattr(self, 'metar_feed') and self.metar_feed:
            await self.metar_feed.stop()
        await self.db.close()

    async def _on_ws_trade(self, data: dict):
        """Callback para trades recibidos via WebSocket en tiempo real."""
        try:
            cid = data.get("market", data.get("conditionId", data.get("asset_id", "")))
            if not cid:
                return
            # Construir trade-like dict para parseo
            trade_data = {
                "conditionId": cid,
                "transactionHash": data.get("id", data.get("transactionHash", "")),
                "proxyWallet": data.get("maker_address", data.get("taker_address", data.get("owner", "unknown"))),
                "side": data.get("side", "BUY"),
                "size": data.get("size", data.get("amount", "0")),
                "price": data.get("price", "0.5"),
                "outcome": data.get("outcome", data.get("asset_id", "Yes")),
                "timestamp": data.get("timestamp", data.get("match_time", "")),
                "title": data.get("title", data.get("market_slug", "")),
            }
            # Reusar _ws_client persistente (no crear uno nuevo por trade)
            if not hasattr(self, '_ws_poly_client') or self._ws_poly_client is None:
                self._ws_poly_client = PolymarketClient()
                self._ws_poly_client._client = __import__('httpx').AsyncClient(timeout=30)
            trade = self._ws_poly_client._parse_trade(trade_data)
            if trade and trade.size > 0:
                await self.process_trade(trade, self._ws_poly_client)
        except Exception as e:
            print(f"[WS] Error procesando trade: {e}", flush=True)

    async def _refresh_ws_subscriptions(self):
        """Actualizar suscripciones WS con mercados activos del último polling."""
        if not self.ws_client or not self._last_markets:
            return
        try:
            market_ids = [m.condition_id for m in self._last_markets[:50]]
            await self.ws_client.subscribe(market_ids)
        except Exception:
            pass

    async def _run_complement_arb(self):
        """v10: Loop background para complement arb scanner."""
        await asyncio.sleep(15)
        while self._running and self.complement_arb:
            try:
                opps = await self.complement_arb.scan()
                if opps:
                    print(f"[ComplementArb] {len(opps)} oportunidades activas", flush=True)
            except Exception as e:
                print(f"[ComplementArb] Error: {e}", flush=True)
            interval = config.COMPLEMENT_ARB_SCAN_INTERVAL or 60
            await asyncio.sleep(interval)

    # ── Polling principal ─────────────────────────────────────────────

    async def run_polling_loop(self):
        """Loop infinito de polling."""
        print(f"Iniciando polling loop (cada {config.POLL_INTERVAL}s)", flush=True)
        cycle = 0
        while self._running:
            try:
                await self.poll_cycle()
                cycle += 1

                # Cada ciclo: scanner dedicado de wallets copy trade
                if self.alert_autotrader:
                    try:
                        await self.alert_autotrader.scan_copy_wallets()
                    except Exception as e:
                        print(f"[CopyScanner] Error: {e}", flush=True)

                # Cada 5 ciclos (~5 min): price impact check + confirmaciones pendientes
                if cycle % 5 == 0:
                    await self.check_price_impact()
                    if self.alert_autotrader:
                        await self.alert_autotrader.check_pending_confirmations()

                # Cada 10 ciclos (~10 min): baselines + smart money scores + kill switch + WS refresh
                if cycle % 10 == 0:
                    await self._refresh_ws_subscriptions()
                    await self.update_all_baselines()
                    sm_count = await self.db.update_all_smart_money_scores()
                    if sm_count:
                        print(f"Smart money scores actualizados: {sm_count} wallets", flush=True)
                    # Kill switch remoto via Telegram
                    await self._check_telegram_commands()

                # Cada 30 ciclos (~30 min): resoluciones + watchlist
                if cycle % 30 == 0:
                    await self.check_resolutions()
                    # Watchlist interno (whale scanner) = smart wallets por score
                    smart_wallets = await self.db.update_watchlist(
                        threshold=config.SMART_MONEY_THRESHOLD,
                        min_resolved=config.COPY_TRADE_MIN_RESOLVED,
                    )
                    # Combinar: smart wallets + favoritos manuales del usuario
                    manual_favs = await self.db.get_watchlisted_wallets()
                    self._watchlist = smart_wallets | manual_favs
                    if self._watchlist:
                        print(f"Watchlist actualizado: {len(self._watchlist)} wallets "
                              f"({len(smart_wallets)} smart + {len(manual_favs)} favs)", flush=True)

                # Cada 15 ciclos (~15 min): on-chain check (20 wallets por batch)
                if cycle % 15 == 0:
                    await self.check_onchain_wallets()

                # Cada 10 ciclos (~10 min): actualizar precios de alertas abiertas
                if cycle % 10 == 0:
                    await self.update_latest_prices()

                # Cada 60 ciclos (~1h): health check + coordination
                if cycle % 60 == 0:
                    await self.send_health_check()
                    try:
                        coord_count = await self.db.detect_coordination()
                        if coord_count:
                            print(f"Coordinacion detectada: {coord_count} pares", flush=True)
                    except Exception as e:
                        print(f"Error en coordinacion: {e}", flush=True)

                # ── v8.0: Módulos adicionales ──
                # Cada 2 ciclos (~2 min): market maker + spike detector
                if cycle % 2 == 0:
                    try:
                        # Market maker viejo deshabilitado — usa crypto_market_maker con loop propio
                        if self.spike_detector and self._last_markets:
                            spike_markets = [{
                                "condition_id": m.condition_id,
                                "market_id": m.condition_id,
                                "price": m.volume_24h / max(m.liquidity, 1) if m.liquidity else 0.5,
                                "question": m.question,
                            } for m in self._last_markets[:50]]
                            spike_signals = await self.spike_detector.tick(spike_markets)
                            if spike_signals:
                                print(f"[SpikeDetector] {len(spike_signals)} spikes detectados", flush=True)
                    except Exception as e:
                        print(f"Error market maker/spike: {e}", flush=True)

                # Cada 3 ciclos (~3 min): event driven + cross platform
                if cycle % 3 == 0:
                    try:
                        if self.event_driven:
                            await self.event_driven.tick()
                        if self.cross_platform:
                            await self.cross_platform.tick()
                    except Exception as e:
                        print(f"Error strategies v8: {e}", flush=True)

                # Cada 5 ciclos: bankroll update
                if cycle % 5 == 0:
                    try:
                        if self.bankroll:
                            await self.bankroll.update_balance()
                    except Exception as e:
                        print(f"Error bankroll: {e}", flush=True)

                # Cada 3 ciclos (~1.5min): mark-to-market de alertas paper abiertas
                if cycle % 3 == 0:
                    try:
                        await self.update_paper_prices()
                    except Exception as e:
                        print(f"Error mark-to-market paper: {e}", flush=True)

                # Cada 120 ciclos (~2h): ML retrain
                if cycle % 120 == 0:
                    try:
                        if self.ml_scorer and config.FEATURE_ML_SCORING:
                            await self.ml_scorer.train()
                    except Exception as e:
                        print(f"Error ML retrain: {e}", flush=True)

            except Exception as e:
                print(f"Error en ciclo polling: {e}", flush=True)
                import traceback
                traceback.print_exc()

            # Crypto arb: procesar y resolver en bloque separado
            # (no depende de poll_cycle — si el polling falla, crypto sigue)
            if config.FEATURE_CRYPTO_ARB:
                try:
                    await self.process_crypto_signals()
                    await self.resolve_crypto_signals()
                    # Risk management: SL/TP/trailing/max holding
                    if self.autotrader:
                        await self.autotrader.check_risk_management()
                    # Resolver autotrades abiertos (mercados cerrados)
                    if self.autotrader:
                        await self.autotrader.resolve_trades()
                    # Auto-claim: reclamar ganancias automáticamente
                    if self.autotrader:
                        await self.autotrader.auto_claim()
                except Exception as e:
                    print(f"Error en crypto arb: {e}", flush=True)

            # Resolver alert autotrades abiertos (mercados largos)
            if self.alert_autotrader:
                try:
                    await self.alert_autotrader.resolve_trades()
                except Exception as e:
                    print(f"Error resolviendo alert autotrades: {e}", flush=True)

            await asyncio.sleep(config.POLL_INTERVAL)

    async def poll_cycle(self):
        print("--- Ciclo de polling ---", flush=True)
        async with PolymarketClient() as client:
            # Pre-cargar cache de mercados para enriquecer trades
            self._last_markets = await client.get_markets(limit=config.MAX_MARKETS) or []

            # Pasar categorías excluidas del dashboard al filtro dinámico
            whale_min = config.WHALE_TRACKER_MIN_SIZE if config.WHALE_TRACKER_ENABLED else 0
            trades = await client.get_recent_trades(
                limit=2000,
                excluded_categories=self._excluded_categories,
                whale_min_size=whale_min,
                watchlisted_wallets=self._watchlist,
            )

            # Guardar stats del pipeline para el dashboard
            self._pipeline_stats = client.get_pipeline_stats()

            # Whale Tracker: guardar trades grandes en DB (antes de filtros normales)
            whale_trades = client.get_last_whale_trades()

            # WhaleScanner: agregar fills para detectar acumulaciones
            if self.whale_scanner and trades:
                try:
                    aggregated = await self.whale_scanner.process_fills(trades)
                    if aggregated:
                        whale_trades = list(whale_trades or []) + aggregated
                except Exception as e:
                    print(f"[WhaleScanner] Error procesando fills: {e}", flush=True)

            if whale_trades:
                saved = 0
                for wt in whale_trades:
                    if await self.db.save_whale_trade(wt):
                        saved += 1
                        # v11: Insider Detection
                        try:
                            ctx = await self.db.get_insider_context(wt.wallet_address, wt.market_id)
                            insider = self.insider_detector.analyze_trade(
                                {"wallet_address": wt.wallet_address, "size": wt.size,
                                 "side": wt.side, "market_id": wt.market_id},
                                ctx)
                            if insider["probability"] >= 15:
                                await self.db.save_insider_flag({
                                    "wallet_address": wt.wallet_address,
                                    "market_id": wt.market_id,
                                    "market_question": wt.market_question,
                                    "trade_size": wt.size,
                                    **insider,
                                })
                        except Exception:
                            pass
                        # v11: Copy Trading
                        try:
                            if self.copy_engine:
                                await self.copy_engine.process_whale_trade(wt)
                        except Exception:
                            pass
                if saved:
                    print(f"[WhaleTracker] {len(whale_trades)} detectados, {saved} nuevos guardados (>=${whale_min:,.0f}$)", flush=True)

            # v11: News Fetcher + Sentiment (cada 15 min internamente)
            try:
                if self.news_fetcher and self._last_markets:
                    markets_dicts = [{"condition_id": m.condition_id, "question": m.question}
                                     for m in self._last_markets[:20]]
                    articles = await self.news_fetcher.fetch_news_for_markets(markets_dicts)
                    # Analizar sentimiento de artículos nuevos
                    if articles:
                        from collections import defaultdict
                        by_market = defaultdict(list)
                        for a in articles:
                            s = self.sentiment_analyzer.analyze_article(a.get("title",""))
                            a["sentiment_score"] = s["score"]
                            a["sentiment_label"] = s["label"]
                            if a.get("market_id"):
                                by_market[a["market_id"]].append(a)
                        # Guardar sentimiento agregado por mercado
                        for mid, arts in by_market.items():
                            agg = self.sentiment_analyzer.analyze_market_news(arts)
                            agg["market_question"] = arts[0].get("market_question","")
                            await self.db.save_market_sentiment(mid, agg)
            except Exception as e:
                print(f"[NewsFetcher] Error: {e}", flush=True)

            # v11: Spike Detection mejorado (correlación con whales)
            try:
                if self.spike_detector and trades:
                    # Construir precios reales desde trades procesados
                    market_prices = {}
                    for t in trades:
                        market_prices[t.market_id] = t.price
                    markets_dicts = [{"condition_id": mid, "price": price,
                                      "question": next((m.question for m in self._last_markets if m.condition_id == mid), "")}
                                     for mid, price in market_prices.items() if price > 0]
                    spikes = await self.spike_detector.tick(markets_dicts)
                    for spike in (spikes or []):
                        # Correlacionar con whale trades
                        whale_info = await self.db.get_whale_trades_for_spike(
                            spike["market_id"], minutes=60)
                        spike["whale_trades_correlated"] = whale_info["count"]
                        spike["whale_volume_correlated"] = whale_info["total_volume"]
                        spike["market_question"] = next(
                            (m.question for m in self._last_markets
                             if m.condition_id == spike["market_id"]), "")
                        spike["old_price"] = spike.get("price", 0) / max(1 + spike.get("spike_pct", 0) / 100, 0.01)
                        spike["new_price"] = spike.get("price", 0)
                        spike["pct_change"] = spike.get("spike_pct", 0)
                        spike["timeframe_min"] = 5
                        await self.db.save_price_spike(spike)
                        if whale_info["count"] > 0:
                            print(f"[Spike] {spike['direction']} {spike['spike_pct']:.1f}% en "
                                  f"{spike['market_question'][:50]} — {whale_info['count']} whales correlacionados", flush=True)
            except Exception as e:
                print(f"[SpikeDetector] Error: {e}", flush=True)

            if not trades:
                print("Trades obtenidos: 0", flush=True)
                return

            sizes = [t.size for t in trades]
            max_s = max(sizes)
            avg_s = sum(sizes) / len(sizes)
            big = sum(1 for s in sizes if s >= config.MIN_SIZE_USD)
            print(
                f"Trades: {len(trades)} | max=${max_s:,.0f} avg=${avg_s:,.0f} | "
                f"{big} >= ${config.MIN_SIZE_USD} (pre-filtro)",
                flush=True,
            )

            for trade in trades:
                await self.process_trade(trade, client)

            # Después del primer ciclo, suscribir WS a mercados activos
            if self.ws_client and self._last_markets:
                await self._refresh_ws_subscriptions()

        logger.info("ciclo_completo", procesados=self.trades_processed, alertas=self.alerts_sent)

    async def process_trade(self, trade, client: PolymarketClient):
        # Skip duplicados
        if await self.db.is_trade_processed(trade.transaction_hash):
            return

        # Registrar trade y wallet siempre
        await self.db.record_trade(trade)
        await self.db.update_wallet_stats(trade)
        self.trades_processed += 1

        # Paper Trading: detectar si wallet cierra posición (SELL = exit)
        if trade.side == "SELL" and trade.market_id and trade.price > 0:
            try:
                closed = await self.db.check_wallet_exit(
                    trade.wallet_address, trade.market_id,
                    trade.outcome, trade.price,
                )
                if closed > 0:
                    print(f"[PaperPnL] Wallet {trade.wallet_address[:10]} cerró {closed} posición(es) "
                          f"en {trade.market_slug} a ${trade.price:.2f}", flush=True)
            except Exception as e:
                print(f"[PaperPnL] Error check_wallet_exit: {e}", flush=True)

        # v10: Volume tracking para spike detection
        if self.alert_autotrader and trade.market_id:
            self.alert_autotrader.track_volume(trade.market_id, trade.size, trade.side)

        # Trackear mercado para resolución futura
        if trade.market_id:
            await self.db.track_market(
                trade.market_id, trade.market_question, trade.market_slug,
                trade.market_end_date, trade.market_category,
            )

        # Skip trades pequeños para análisis (excepto wallets en watchlist)
        is_watched = trade.wallet_address.lower() in self._watchlist
        if trade.size < config.MIN_SIZE_USD and not is_watched:
            self._debug["too_small"] += 1
            return

        # Obtener contexto — envuelto en try/except para no abortar el ciclo
        try:
            wallet_stats = await self.db.get_wallet_stats(trade.wallet_address)
            market_baseline = await self.db.get_market_baseline(trade.market_id)
        except Exception as e:
            err_msg = f"contexto DB: {e} | wallet={trade.wallet_address[:12]} market={trade.market_slug}"
            print(f"ERROR {err_msg}", flush=True)
            self._debug["errors"] = self._debug.get("errors", 0) + 1
            self._debug["last_error"] = err_msg
            return

        # Clustering: buscar wallets que apostaron igual recientemente
        cluster = []
        try:
            cluster = await self.db.find_cluster_wallets(
                trade.market_id, trade.side, trade.outcome,
                window_minutes=30, min_size=1000,
            )
        except Exception:
            pass

        # Enriquecer trade con datos del mercado si faltan
        if not trade.market_end_date and trade.market_id:
            try:
                market_data = await client.get_market_by_id(trade.market_id)
                if market_data and market_data.get("end_date"):
                    trade.market_end_date = market_data["end_date"]
            except Exception:
                pass

        # v4.0: Contexto adicional para nuevas señales
        accumulation_info = None
        market_price = None
        outcome_price = None
        smart_cluster_count = 0
        try:
            accumulation_info = await self.db.get_accumulation_info(
                trade.wallet_address, trade.market_id, trade.outcome)
            market_price = await client.get_market_price(trade.market_id, "Yes")
            outcome_price = await client.get_market_price(trade.market_id, trade.outcome or "Yes") if (trade.outcome or "Yes") != "Yes" else market_price
            smart_cluster_count = await self.db.count_smart_wallets_same_side(
                trade.market_id, trade.side, trade.outcome, trade.wallet_address)
        except Exception:
            pass

        # v6.0: Wallet Baskets — detectar si wallet opera fuera de categoría
        wallet_category_shift = False
        cross_basket_count = 0
        if config.FEATURE_WALLET_BASKETS and trade.market_category:
            try:
                profile = await self.db.get_wallet_category_profile(trade.wallet_address)
                if profile["total_trades"] >= config.BASKET_MIN_WALLET_TRADES:
                    cat_info = profile["categories"].get(trade.market_category)
                    cat_pct = (cat_info["pct"] / 100) if cat_info else 0
                    if cat_pct < config.BASKET_CATEGORY_SHIFT_THRESHOLD:
                        wallet_category_shift = True
                    # Cuántas wallets fuera de categoría operan en este mercado
                    cross_wallets = await self.db.find_basket_wallets_in_market(
                        trade.market_id, trade.market_category)
                    cross_basket_count = len(cross_wallets)
            except Exception:
                pass

        # v6.0: Sniper DBSCAN — detectar cluster temporal estrecho
        sniper_cluster_size = 0
        if config.FEATURE_SNIPER_DBSCAN:
            try:
                trades_nearby = await self.db.get_trades_for_sniper_scan(
                    trade.market_id, trade.side, trade.outcome,
                    window_minutes=config.SNIPER_SCAN_WINDOW_MIN,
                    min_size=config.SNIPER_MIN_TRADE_SIZE,
                )
                if len(trades_nearby) >= config.SNIPER_MIN_CLUSTER_SIZE:
                    # DBSCAN simplificado: agrupar trades por ventana temporal
                    sniper_cluster_size = self._dbscan_cluster(
                        trades_nearby, config.SNIPER_TIME_WINDOW_SEC)
            except Exception:
                pass

        # v8.0: News Catalyst — buscar menciones en noticias
        news_mentions = 0
        if config.FEATURE_NEWS_CATALYST and hasattr(self, 'news_catalyst') and self.news_catalyst:
            try:
                news_result = await self.news_catalyst.check_news_for_market(trade.market_question or "")
                news_mentions = news_result.get("mentions", 0)
            except Exception:
                pass

        # v8.0: ML Scoring — predecir probabilidad de acierto
        ml_prediction = None
        if config.FEATURE_ML_SCORING and hasattr(self, 'ml_scorer') and self.ml_scorer:
            try:
                ml_prediction = self.ml_scorer.predict({
                    "score": 0,  # se recalcula después, pero damos contexto
                    "wallet_trades": wallet_stats.total_trades if wallet_stats else 0,
                    "wallet_winrate": wallet_stats.hit_rate if wallet_stats else 0,
                    "market_volume": market_baseline.total_volume if market_baseline else 0,
                    "size_usd": trade.size,
                    "num_triggers": 0,
                    "hour_of_day": trade.timestamp.hour if trade.timestamp else 12,
                    "is_contrarian": 1 if market_price and market_price < 0.2 else 0,
                    "has_cluster": 1 if cluster and len(cluster) >= 3 else 0,
                    "is_accumulation": 1 if accumulation_info and accumulation_info.get("count", 0) >= 2 else 0,
                })
            except Exception:
                pass

        # Analizar con todas las señales (v6.0 + v8.0)
        try:
            candidate = self.analyzer.analyze(
                trade, wallet_stats, market_baseline, cluster,
                accumulation_info=accumulation_info,
                market_price=market_price,
                smart_cluster_count=smart_cluster_count,
                wallet_category_shift=wallet_category_shift,
                cross_basket_count=cross_basket_count,
                sniper_cluster_size=sniper_cluster_size,
                news_mentions=news_mentions,
                ml_prediction=ml_prediction,
            )
        except Exception as e:
            err_msg = f"analyzer: {e} | size=${trade.size} wallet={trade.wallet_address[:12]} market={trade.market_slug}"
            print(f"ERROR {err_msg}", flush=True)
            self._debug["errors"] = self._debug.get("errors", 0) + 1
            self._debug["last_error"] = err_msg
            return

        # Copy-trade: si wallet está en watchlist, alertar aunque score sea bajo
        is_copy = trade.wallet_address.lower() in self._watchlist
        should_alert = self.analyzer.should_alert(candidate) or is_copy

        # Escribir score en whale_trades si el trade es whale-sized
        if trade.size >= (config.WHALE_TRACKER_MIN_SIZE if config.WHALE_TRACKER_ENABLED else 0):
            try:
                await self.db.update_whale_trade_score(trade.transaction_hash, candidate.score)
            except Exception:
                pass

        self._debug["scored"] += 1
        self._debug["last_scores"] = (self._debug["last_scores"] + [{
            "score": candidate.score, "size": trade.size,
            "triggers": candidate.triggers[:3], "market": (trade.market_question or "")[:50],
            "slug": (trade.market_slug or "")[:40], "cat": trade.market_category or "",
        }])[-20:]

        if not should_alert:
            self._debug["low_score"] += 1
            return

        # Filtrar por categorías excluidas (cacheadas en memoria)
        # Copy trades bypasean filtro de categorías — copiamos lo que la wallet haga
        if self._excluded_categories and not is_copy:
            excluded_cats = self._excluded_categories
            market_cat = (trade.market_category or "").lower()
            if market_cat and market_cat in excluded_cats:
                self._debug["excluded_cat"] += 1
                print(f"DEBUG EXCLUDED cat={market_cat} slug={trade.market_slug} score={candidate.score}", flush=True)
                return
            market_slug_lower = (trade.market_slug or "").lower()
            matched_slug = [cat for cat in excluded_cats if len(cat) > 2 and cat in market_slug_lower]
            if matched_slug:
                self._debug["excluded_cat"] += 1
                print(f"DEBUG EXCLUDED slug_match={matched_slug} slug={trade.market_slug} score={candidate.score}", flush=True)
                return
            q_lower = (trade.market_question or "").lower()
            sport_keywords = {"nba", "nfl", "nhl", "mlb", "mls", "soccer", "football", "basketball", "baseball", "hockey", "esports"}
            matched_keywords = excluded_cats & sport_keywords
            if any(kw in q_lower for kw in matched_keywords):
                self._debug["excluded_cat"] += 1
                print(f"DEBUG EXCLUDED keyword in question: {trade.market_question[:60]} score={candidate.score}", flush=True)
                return

        # Cooldown — copy trades bypasean cooldown para no perder trades de wallets seguidas
        if not is_copy and not await self.db.should_alert(trade.wallet_address, trade.market_id, config.COOLDOWN_HOURS):
            self._debug["cooldown"] += 1
            print(f"DEBUG COOLDOWN wallet={trade.wallet_address[:10]} market={trade.market_slug} score={candidate.score}", flush=True)
            return

        # Enviar alerta (copy-trade o anomalía)
        if is_copy and not self.analyzer.should_alert(candidate):
            candidate.triggers.insert(0, "⭐ Smart Money (watchlisted)")
            success = await self.notifier.send_copy_trade_alert(trade, candidate)
        else:
            if is_copy:
                candidate.triggers.insert(0, "⭐ Smart Money")
            success = await self.notifier.send_alert(candidate)

        if success:
            self.alerts_sent += 1
            # Registrar alerta para TODOS los usuarios (no solo admin)
            await self.db.record_alert_for_all_users(
                wallet=trade.wallet_address,
                market_id=trade.market_id,
                market_question=trade.market_question,
                market_slug=trade.market_slug,
                side=trade.side,
                outcome=trade.outcome,
                size=trade.size,
                price=trade.price,
                score=candidate.score,
                triggers=candidate.triggers,
                cluster_wallets=candidate.cluster_wallets,
                days_to_close=candidate.days_to_resolution,
                wallet_hit_rate=candidate.wallet_hit_rate,
                price_at_alert=outcome_price or market_price or trade.price,
                is_copy_trade=is_copy,
            )
            logger.info("alerta_enviada",
                        wallet=trade.wallet_address[:10],
                        market=trade.market_slug,
                        score=candidate.score,
                        copy_trade=is_copy)

            # v8.0: Push Notifications — guardar para TODOS los usuarios
            if config.FEATURE_PUSH_NOTIFICATIONS and candidate.score >= config.PUSH_MIN_SCORE:
                try:
                    level = "critical" if candidate.score >= 12 else "high" if candidate.score >= 8 else "medium"
                    notif_title = f"Alerta Score {candidate.score}"
                    notif_body = f"{trade.market_question[:80]} | ${trade.size:,.0f} | {', '.join(candidate.triggers[:3])}"
                    # Notificar a todos los usuarios
                    all_users = await self.db.get_all_active_users()
                    for u in (all_users or [{"id": 1}]):
                        await self.db.create_notification(
                            user_id=u["id"],
                            ntype=level,
                            title=notif_title,
                            body=notif_body,
                        )
                except Exception:
                    pass

            # Alert AutoTrader: evaluar copy-trade automático
            if self.alert_autotrader:
                try:
                    await self.alert_autotrader.process_alert(candidate, trade, is_copy_trade=is_copy)
                except Exception as e:
                    print(f"[AlertTrader] Error en process_alert: {e}", flush=True)

    # ── Sniper DBSCAN ──────────────────────────────────────────────────

    def _dbscan_cluster(self, trades: list[dict], time_window_sec: float) -> int:
        """DBSCAN simplificado: encontrar el cluster más grande de trades
        donde cada trade está a menos de time_window_sec del siguiente.
        Retorna el tamaño del cluster más grande (wallets únicas).
        """
        if not trades:
            return 0
        # Extraer timestamps y wallets
        points = [(float(t["ts_epoch"]), t["wallet_address"]) for t in trades if t.get("ts_epoch")]
        if len(points) < 2:
            return 0
        points.sort(key=lambda x: x[0])
        # Encontrar clusters de trades cercanos en tiempo
        clusters = []
        current_cluster = {points[0][1]}
        last_ts = points[0][0]
        for i in range(1, len(points)):
            ts, wallet = points[i]
            if ts - last_ts <= time_window_sec:
                current_cluster.add(wallet)
            else:
                if len(current_cluster) >= 2:
                    clusters.append(current_cluster)
                current_cluster = {wallet}
            last_ts = ts
        if len(current_cluster) >= 2:
            clusters.append(current_cluster)
        if not clusters:
            return 0
        return max(len(c) for c in clusters)

    # ── Paper Trading: Mark-to-Market ────────────────────────────────

    async def update_paper_prices(self):
        """Actualizar precio actual de alertas abiertas (mark-to-market)."""
        try:
            open_alerts = await self.db.get_open_paper_alerts()
            if not open_alerts:
                print("[PaperPnL] No hay alertas abiertas para mark-to-market", flush=True)
                return

            # Agrupar por (market_id, outcome) para obtener precio correcto por token
            pairs = list({(a["market_id"], a.get("outcome", "Yes")) for a in open_alerts if a.get("market_id")})
            if not pairs:
                print("[PaperPnL] Alertas abiertas sin market_id", flush=True)
                return

            print(f"[PaperPnL] Mark-to-market: {len(open_alerts)} alertas, {len(pairs)} pares (mercado+outcome)", flush=True)
            updated = 0
            failed = 0
            async with PolymarketClient() as client:
                for mid, outcome in pairs[:50]:  # Max 50 pares por ciclo
                    try:
                        price = await client.get_market_price(mid, outcome)
                        if price is not None:
                            async with self.db._pool.acquire() as conn:
                                await conn.execute("""
                                    UPDATE alerts SET price_latest = $1, price_latest_at = NOW()
                                    WHERE market_id = $2 AND outcome = $3
                                      AND resolved = FALSE AND exit_type IS NULL
                                """, price, mid, outcome)
                                updated += 1
                        else:
                            failed += 1
                    except Exception as e:
                        failed += 1
                        print(f"[PaperPnL] Error precio {mid[:12]}({outcome}): {e}", flush=True)

            print(f"[PaperPnL] Mark-to-market: {updated} OK, {failed} fallidos de {len(pairs)}", flush=True)
        except Exception as e:
            print(f"[PaperPnL] Error update_paper_prices: {e}", flush=True)

    # ── Baselines ─────────────────────────────────────────────────────

    async def update_all_baselines(self):
        """Actualizar baselines de todos los mercados con trades recientes."""
        try:
            async with self.db._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT DISTINCT market_id FROM trades
                    WHERE timestamp > NOW() - INTERVAL '24 hours'
                """)
            for r in rows:
                await self.db.update_market_baseline(r["market_id"])
            print(f"Baselines actualizados: {len(rows)} mercados", flush=True)
        except Exception as e:
            print(f"Error actualizando baselines: {e}", flush=True)

    # ── Resolution Checker ────────────────────────────────────────────

    async def check_resolutions(self):
        """Verificar si mercados trackeados se resolvieron."""
        try:
            unresolved = await self.db.get_unresolved_markets()
            if not unresolved:
                return

            print(f"Verificando resolución de {len(unresolved)} mercados...", flush=True)
            resolved_count = 0

            async with PolymarketClient() as client:
                for market in unresolved:
                    resolution = await client.check_market_resolution(market["condition_id"])
                    if resolution:
                        await self.db.resolve_market(market["condition_id"], resolution)
                        resolved_count += 1

                        # Contar alertas correctas para este mercado
                        alerts = await self.db.get_market_alerts(market["condition_id"])
                        correct = sum(1 for a in alerts if a.get("was_correct"))
                        total = len(alerts)

                        if total > 0:
                            await self.notifier.send_resolution_update(
                                market.get("question", ""),
                                resolution, correct, total,
                            )

            if resolved_count > 0:
                print(f"Mercados resueltos: {resolved_count}", flush=True)
        except Exception as e:
            print(f"Error verificando resoluciones: {e}", flush=True)

    # ── Kill Switch Telegram ─────────────────────────────────────────

    async def _check_telegram_commands(self):
        """Chequear comandos de Telegram para kill switch remoto."""
        try:
            cmd = await self.notifier.check_commands()
            if not cmd:
                return
            if cmd == "stop":
                # Desactivar ambos autotraders
                if self.alert_autotrader and self.alert_autotrader._enabled:
                    self.alert_autotrader._enabled = False
                    await self.db.set_config_bulk({"aat_enabled": "false"})
                if self.autotrader and self.autotrader._enabled:
                    self.autotrader._enabled = False
                    await self.db.set_config_bulk({"at_enabled": "false"})
                await self.notifier.send_kill_switch_alert("stop", "Comando /stop recibido")
                print("[KillSwitch] Trading DETENIDO via Telegram", flush=True)
            elif cmd == "start":
                await self.db.set_config_bulk({"aat_enabled": "true"})
                if self.alert_autotrader:
                    await self.alert_autotrader.reload_config()
                await self.db.set_config_bulk({"at_enabled": "true"})
                if self.autotrader:
                    await self.autotrader.reload_config()
                await self.notifier.send_kill_switch_alert("start", "Comando /start recibido")
                print("[KillSwitch] Trading REANUDADO via Telegram", flush=True)
            elif cmd == "status":
                aat_status = self.alert_autotrader.get_status() if self.alert_autotrader else {}
                msg = (
                    f"📊 <b>Status</b>\n\n"
                    f"Alert Trading: {'✅ ON' if aat_status.get('enabled') else '❌ OFF'}\n"
                    f"Posiciones: {aat_status.get('open_positions', 0)}\n"
                    f"PnL hoy: ${aat_status.get('pnl_today', 0):.2f}\n"
                    f"Drawdown pausado: {'Sí' if aat_status.get('drawdown_paused') else 'No'}"
                )
                await self.notifier._send_to_all(msg)
        except Exception as e:
            print(f"[KillSwitch] Error: {e}", flush=True)

    # ── Price Impact Checker ──────────────────────────────────────────

    async def check_price_impact(self):
        """Verificar cómo se movió el precio después de cada alerta."""
        try:
            checks = [
                ("price_1h", 1, 48),
                ("price_6h", 6, 48),
                ("price_24h", 24, 72),
            ]
            updated = 0
            async with PolymarketClient() as client:
                for field, min_h, max_h in checks:
                    alerts = await self.db.get_alerts_needing_price_check(field, min_h, max_h)
                    for alert in alerts:
                        price = await client.get_market_price(
                            alert["market_id"], alert.get("outcome", "Yes")
                        )
                        if price is not None:
                            await self.db.update_alert_price_impact(alert["id"], field, price)
                            updated += 1
            if updated:
                print(f"Price impact: {updated} alertas actualizadas", flush=True)
        except Exception as e:
            print(f"Error en price impact check: {e}", flush=True)

    async def update_latest_prices(self):
        """Actualizar price_latest de alertas abiertas (cada ~1h)."""
        try:
            alerts = await self.db.get_alerts_for_latest_price(limit=50)
            if not alerts:
                return
            updated = 0
            async with PolymarketClient() as client:
                for alert in alerts:
                    price = await client.get_market_price(
                        alert["market_id"], alert.get("outcome", "Yes")
                    )
                    if price is not None:
                        await self.db.update_alert_price_latest(alert["id"], price)
                        updated += 1
            if updated:
                print(f"Price latest: {updated} alertas actualizadas", flush=True)
        except Exception as e:
            print(f"Error en update_latest_prices: {e}", flush=True)

    # ── On-chain Checker (Polygonscan) ─────────────────────────────

    async def check_onchain_wallets(self):
        """Verificar datos on-chain de wallets nuevas (rate-limited)."""
        if not config.POLYGONSCAN_API_KEY:
            return
        try:
            wallets = await self.db.get_wallets_for_onchain_check(limit=20)
            for addr in wallets:
                info = await get_wallet_onchain_info(addr)
                # Guardar si hay al menos algún dato útil
                has_data = any(v is not None for v in info.values())
                if has_data:
                    await self.db.update_wallet_onchain(addr, info)
                await asyncio.sleep(2)  # Rate limit entre wallets
            if wallets:
                print(f"On-chain check: {len(wallets)} wallets", flush=True)
        except Exception as e:
            print(f"Error en on-chain check: {e}", flush=True)

    # ── Crypto Arb Signal Processing ────────────────────────────────

    async def process_crypto_signals(self):
        """Procesar señales del detector crypto y guardarlas en DB + Telegram."""
        if not self.crypto_detector:
            return
        try:
            signals = self.crypto_detector.get_recent_signals(200)
            # Merge señales early entry
            if self.early_detector:
                early = self.early_detector.get_recent_signals(100)
                if early:
                    existing_cids = {s["condition_id"] for s in signals}
                    for es in early:
                        if es["condition_id"] not in existing_cids:
                            signals.append(es)
                            existing_cids.add(es["condition_id"])
            if not signals:
                return
            # Cargar condition_ids ya guardados (una sola query)
            existing = await self.db.get_crypto_signals_history(limit=500)
            existing_cids = {e["condition_id"] for e in existing}

            saved = 0
            for sig in signals:
                if sig["condition_id"] in existing_cids:
                    continue

                # Guardar en DB
                sig["paper_bet_size"] = config.CRYPTO_ARB_PAPER_BET
                await self.db.record_crypto_signal(sig)
                existing_cids.add(sig["condition_id"])
                saved += 1

                # Enviar a Telegram
                if config.CRYPTO_ARB_TELEGRAM:
                    await self.notifier.send_crypto_signal(sig)

            if saved:
                print(f"Crypto signals guardadas: {saved}", flush=True)

            # Nota: el autotrader evalúa señales en su propio loop rápido
            # (_run_crypto_autotrader cada 5s) para no perder señales de corta vida
        except Exception as e:
            print(f"Error procesando crypto signals: {e}", flush=True)

    async def resolve_crypto_signals(self):
        """Resolver señales crypto pendientes.

        CLOB API tarda horas en marcar mercados 5-min como cerrados.
        Usamos Gamma API por slug (rápido) o CLOB API como fallback.
        """
        try:
            unresolved = await self.db.get_unresolved_crypto_signals()
            if not unresolved:
                return

            resolved_count = 0

            async with httpx.AsyncClient(timeout=15) as client:
                for sig in unresolved:
                    cid = sig["condition_id"]
                    direction = sig.get("direction", "").lower()
                    coin = sig.get("coin", "BTC")
                    created = sig.get("created_at")
                    time_rem = sig.get("time_remaining_sec", 300)

                    # Calcular cuándo cerró el mercado
                    if not created:
                        continue
                    time_rem = time_rem or 0  # Asegurar que 0 no sea None
                    if isinstance(created, str):
                        try:
                            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        except Exception:
                            continue
                    end_time = created + timedelta(seconds=int(time_rem))
                    now = datetime.now(end_time.tzinfo)
                    # Si el mercado aún no cerró, saltar
                    if now < end_time:
                        continue
                    secs_since_close = (now - end_time).total_seconds()

                    # Si cerró hace menos de 20s, esperar un poco para que se actualice
                    if secs_since_close < 20:
                        continue

                    resolution = None

                    # Método 1: Gamma API por slug del evento
                    # Primero usar el event_slug guardado, luego recalcular
                    event_slug = sig.get("event_slug", "")
                    slugs_to_try = []
                    if event_slug:
                        slugs_to_try.append(event_slug)
                    # Fallback: recalcular slugs posibles
                    end_ts = int(end_time.timestamp())
                    coin_prefix = coin.lower()
                    for dur, interval in [("5m", 300), ("15m", 900)]:
                        start_ts = end_ts - interval
                        start_ts = (start_ts // interval) * interval
                        calc_slug = f"{coin_prefix}-updown-{dur}-{start_ts}"
                        if calc_slug not in slugs_to_try:
                            slugs_to_try.append(calc_slug)
                        # También probar +/- 1 intervalo por si hay desfase
                        for adj in [-interval, interval]:
                            adj_slug = f"{coin_prefix}-updown-{dur}-{start_ts + adj}"
                            if adj_slug not in slugs_to_try:
                                slugs_to_try.append(adj_slug)

                    try:
                        for slug in slugs_to_try:
                            resp = await client.get(
                                f"{config.GAMMA_API_URL}/events",
                                params={"slug": slug},
                            )
                            if resp.status_code != 200:
                                continue
                            events = resp.json()
                            if not events:
                                continue
                            ev = events[0]
                            for m in ev.get("markets", []):
                                m_cid = m.get("conditionId", "")
                                if m_cid == cid and m.get("closed"):
                                    prices = m.get("outcomePrices", "")
                                    if isinstance(prices, str):
                                        try:
                                            prices = __import__("json").loads(prices)
                                        except Exception:
                                            prices = []
                                    outcomes = m.get("outcomes", "")
                                    if isinstance(outcomes, str):
                                        try:
                                            outcomes = __import__("json").loads(outcomes)
                                        except Exception:
                                            outcomes = []
                                    if prices and outcomes:
                                        for i, p in enumerate(prices):
                                            if float(p) >= 0.95 and i < len(outcomes):
                                                resolution = outcomes[i]
                                                break
                            if resolution:
                                break
                    except Exception as e:
                        print(f"[CryptoResolve] Gamma error para {cid[:12]}: {e}", flush=True)

                    # Método 2: CLOB API (puede funcionar para mercados más viejos)
                    if not resolution:
                        try:
                            resp2 = await client.get(
                                f"{config.CLOB_API_URL}/markets/{cid}"
                            )
                            if resp2.status_code == 200:
                                data = resp2.json()
                                if data.get("closed"):
                                    for tok in data.get("tokens", []):
                                        if tok.get("winner") is True:
                                            resolution = tok.get("outcome", "")
                                            break
                        except Exception:
                            pass

                    # Método 3 ELIMINADO: Binance auto-resolve era incorrecto.
                    # Polymarket compara contra "price to beat" (inicio del mercado),
                    # no contra el spot_price cuando se detectó la señal.
                    # Solo confiamos en la resolución oficial de Gamma/CLOB.

                    if not resolution:
                        continue

                    # Calcular resultado
                    won = resolution.lower() == direction
                    paper_result = "win" if won else "loss"
                    bet = float(sig.get("paper_bet_size", config.CRYPTO_ARB_PAPER_BET))
                    odds = float(sig.get("poly_odds", 0.5))
                    paper_pnl = bet * ((1.0 / odds) - 1) if won else -bet

                    await self.db.resolve_crypto_signal(
                        sig["id"], resolution, paper_result, round(paper_pnl, 2)
                    )
                    resolved_count += 1

                    if self.crypto_detector:
                        self.crypto_detector.resolve_signal(cid, paper_result, round(paper_pnl, 2))
                    if self.early_detector:
                        self.early_detector.resolve_signal(cid, paper_result, round(paper_pnl, 2))

                    print(f"[CryptoResolve] {coin} {direction} → {resolution} = {paper_result} "
                          f"(${paper_pnl:.2f})", flush=True)

            if resolved_count:
                print(f"Crypto signals resueltas: {resolved_count}", flush=True)
        except Exception as e:
            print(f"Error resolviendo crypto signals: {e}", flush=True)

    # ── Health Check ──────────────────────────────────────────────────

    async def send_health_check(self):
        try:
            stats = await self.db.get_dashboard_stats()
            uptime = datetime.now() - self.start_time
            hours = int(uptime.total_seconds() // 3600)
            minutes = int((uptime.total_seconds() % 3600) // 60)
            stats["uptime"] = f"{hours}h {minutes}m"
            await self.notifier.send_health_check(stats)
        except Exception as e:
            print(f"Error en health check: {e}", flush=True)


# ── FastAPI App ──────────────────────────────────────────────────────

bot: PolymarketAlertBot | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle: iniciar bot + polling en background, cerrar al apagar."""
    global bot
    bot = PolymarketAlertBot()
    await bot.start()
    app.state.db = bot.db
    app.state.bot = bot
    app.state.autotrader = bot.autotrader
    app.state.alert_autotrader = bot.alert_autotrader
    app.state.crypto_detector = bot.crypto_detector
    # v8.0 módulos
    app.state.market_maker = bot.market_maker
    app.state.spike_detector = bot.spike_detector
    app.state.event_driven = bot.event_driven
    app.state.cross_platform = bot.cross_platform
    app.state.rate_limiter = bot.rate_limiter
    app.state.queue = bot.signal_queue
    app.state.websocket = bot.ws_client
    app.state.bankroll = bot.bankroll
    app.state.news_catalyst = bot.news_catalyst
    app.state.ml_scorer = bot.ml_scorer
    # v10.0 módulos
    app.state.complement_arb = bot.complement_arb
    # Lanzar polling en background
    polling_task = asyncio.create_task(bot.run_polling_loop())
    print(f"Dashboard activo en puerto {config.DASHBOARD_PORT}", flush=True)
    yield
    # Shutdown
    bot._running = False
    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass
    await bot.stop()


app = FastAPI(title="Polymarket Insider Alert Bot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


if __name__ == "__main__":
    print(f"=== INICIANDO SERVER EN PUERTO {config.DASHBOARD_PORT} ===", flush=True)
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=config.DASHBOARD_PORT,
        log_level="info",
    )
