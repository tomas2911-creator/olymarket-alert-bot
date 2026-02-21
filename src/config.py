"""Configuración centralizada del bot."""
import os
from pathlib import Path
from dotenv import load_dotenv
import yaml

load_dotenv()


def load_yaml_config() -> dict:
    """Cargar configuración desde config.yaml."""
    config_path = Path(__file__).parent.parent / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


_yaml = load_yaml_config()

# === Telegram ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_ID", "")

# === PostgreSQL ===
DATABASE_URL = os.getenv("DATABASE_URL", "")
# Railway usa postgres:// pero asyncpg necesita postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# === Polymarket API ===
GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"

# === Polling ===
_polling = _yaml.get("polling", {})
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", _polling.get("interval_seconds", 30)))
MAX_MARKETS = _polling.get("max_markets", 50)

# === Detection ===
_detection = _yaml.get("detection", {})
FRESH_WALLET_MAX_TRADES = _detection.get("fresh_wallet_max_trades", 5)
FRESH_WALLET_MAX_DAYS = _detection.get("fresh_wallet_max_days", 30)
MIN_SIZE_USD = int(os.getenv("MIN_SIZE_USD", _detection.get("min_size_usd", 50)))
LARGE_SIZE_USD = int(os.getenv("LARGE_SIZE_USD", _detection.get("large_size_usd", 200)))
MARKET_PERCENTILE = _detection.get("market_percentile", 95)
WALLET_SIZE_MULTIPLIER = _detection.get("wallet_size_multiplier", 5)
CONCENTRATION_THRESHOLD_PCT = _detection.get("concentration_threshold_pct", 10)

# === Scoring ===
_scoring = _yaml.get("scoring", {})
FRESH_WALLET_POINTS = _scoring.get("fresh_wallet_points", 2)
LARGE_SIZE_POINTS = _scoring.get("large_size_points", 2)
MARKET_ANOMALY_POINTS = _scoring.get("market_anomaly_points", 2)
WALLET_SHIFT_POINTS = _scoring.get("wallet_shift_points", 3)
CONCENTRATION_POINTS = _scoring.get("concentration_points", 3)
TIME_PROXIMITY_POINTS = _scoring.get("time_proximity_points", 3)
CLUSTER_POINTS = _scoring.get("cluster_points", 3)
ALERT_THRESHOLD = int(os.getenv("ALERT_THRESHOLD", _scoring.get("alert_threshold", 5)))

# === Señales 8-14 (antes hardcoded) ===
HIT_RATE_MIN_RESOLVED = _scoring.get("hit_rate_min_resolved", 3)
HIT_RATE_MIN_PCT = _scoring.get("hit_rate_min_pct", 70)
HIT_RATE_POINTS = _scoring.get("hit_rate_points", 2)
CONTRARIAN_POINTS = _scoring.get("contrarian_points", 3)
ACCUMULATION_POINTS = _scoring.get("accumulation_points", 2)
PROVEN_WINNER_MIN_RESOLVED = _scoring.get("proven_winner_min_resolved", 5)
PROVEN_WINNER_MIN_PCT = _scoring.get("proven_winner_min_pct", 65)
PROVEN_WINNER_POINTS = _scoring.get("proven_winner_points", 3)
MULTI_SMART_POINTS = _scoring.get("multi_smart_points", 3)
LATE_INSIDER_POINTS = _scoring.get("late_insider_points", 2)
EXIT_ALERT_MIN_RESOLVED = _scoring.get("exit_alert_min_resolved", 3)
EXIT_ALERT_MIN_PCT = _scoring.get("exit_alert_min_pct", 60)
EXIT_ALERT_POINTS = _scoring.get("exit_alert_points", 2)
CROSS_BASKET_EXTRA_POINTS = _scoring.get("cross_basket_extra_points", 2)

# === Alerts ===
_alerts = _yaml.get("alerts", {})
COOLDOWN_HOURS = _alerts.get("cooldown_hours", 6)
MAX_ALERTS_PER_HOUR = _alerts.get("max_alerts_per_hour", 20)

# === Smart Money ===
SMART_MONEY_THRESHOLD = int(os.getenv("SMART_MONEY_THRESHOLD", 50))
COPY_TRADE_MIN_RESOLVED = int(os.getenv("COPY_TRADE_MIN_RESOLVED", 3))
SMART_WALLET_MIN_WINRATE = 0.55  # Win rate mínimo para contar como "smart wallet"

# === Polygonscan (opcional) ===
POLYGONSCAN_API_KEY = os.getenv("POLYGONSCAN_API_KEY", "")

# === Builder API (para auto-claim de posiciones ganadoras) ===
BUILDER_KEY = os.getenv("BUILDER_KEY", "")
BUILDER_SECRET = os.getenv("BUILDER_SECRET", "")
BUILDER_PASSPHRASE = os.getenv("BUILDER_PASSPHRASE", "")

# === Feature Flags ===
_features = _yaml.get("features", {})
FEATURE_ORDERBOOK_DEPTH = _features.get("orderbook_depth", False)
FEATURE_MARKET_CLASSIFICATION = _features.get("market_classification", False)
FEATURE_WALLET_BASKETS = _features.get("wallet_baskets", False)
FEATURE_SNIPER_DBSCAN = _features.get("sniper_dbscan", False)
FEATURE_CRYPTO_ARB = _features.get("crypto_arb", False)
FEATURE_CONSENSUS_SHIFT = _features.get("consensus_shift", True)
FEATURE_RESOLUTION_PATTERN = _features.get("resolution_pattern", True)

# === Señales v7.0 (#19-#22) ===
_v7 = _yaml.get("signals_v7", {})
CONSENSUS_SHIFT_MIN_PCT = _v7.get("consensus_shift_min_pct", 15)
CONSENSUS_SHIFT_MAX_HOURS = _v7.get("consensus_shift_max_hours", 6)
CONSENSUS_SHIFT_POINTS = _v7.get("consensus_shift_points", 3)
RESOLUTION_PATTERN_MIN_TRADES = _v7.get("resolution_pattern_min_trades", 3)
RESOLUTION_PATTERN_MAX_DAYS = _v7.get("resolution_pattern_max_days", 2)
RESOLUTION_PATTERN_POINTS = _v7.get("resolution_pattern_points", 3)
WHALE_ALERT_MIN_SIZE = _v7.get("whale_alert_min_size", 5000)
WHALE_ALERT_POINTS = _v7.get("whale_alert_points", 3)
REPEAT_WINNER_MIN_WINS = _v7.get("repeat_winner_min_wins", 3)
REPEAT_WINNER_POINTS = _v7.get("repeat_winner_points", 2)

# === Scoring (nuevos) ===
ORDERBOOK_DEPTH_POINTS = _scoring.get("orderbook_depth_points", 2)
NICHE_MARKET_POINTS = _scoring.get("niche_market_points", 2)

# === Order Book ===
_orderbook = _yaml.get("orderbook", {})
ORDERBOOK_MIN_DEPTH_PCT = _orderbook.get("min_depth_pct", 2.0)
ORDERBOOK_CACHE_TTL = _orderbook.get("cache_ttl_seconds", 30)

# === Market Classification ===
_mclass = _yaml.get("market_classification", {})
NICHE_MAX_LIQUIDITY = _mclass.get("niche_max_liquidity", 50000)
NICHE_SCORE_MULTIPLIER = _mclass.get("niche_score_multiplier", 1.5)
MAINSTREAM_CATEGORIES = set(_mclass.get("mainstream_categories", ["politics", "sports", "crypto-prices"]))

# === Wallet Baskets ===
_baskets = _yaml.get("wallet_baskets", {})
BASKET_MIN_WALLET_TRADES = _baskets.get("min_wallet_trades", 5)
BASKET_CATEGORY_SHIFT_THRESHOLD = _baskets.get("category_shift_threshold", 0.15)
BASKET_POINTS = _baskets.get("basket_points", 3)
BASKET_CROSS_MIN = _baskets.get("cross_basket_min", 2)

# === Whale Tracker ===
_whale_tracker = _yaml.get("whale_tracker", {})
WHALE_TRACKER_MIN_SIZE = _whale_tracker.get("min_size", 10000)
WHALE_TRACKER_ENABLED = _whale_tracker.get("enabled", True)

# === Sniper DBSCAN ===
_sniper = _yaml.get("sniper_dbscan", {})
SNIPER_TIME_WINDOW_SEC = _sniper.get("time_window_sec", 120)
SNIPER_MIN_CLUSTER_SIZE = _sniper.get("min_cluster_size", 3)
SNIPER_MIN_TRADE_SIZE = _sniper.get("min_trade_size", 500)
SNIPER_SCAN_WINDOW_MIN = _sniper.get("scan_window_minutes", 10)
SNIPER_POINTS = _sniper.get("sniper_points", 4)

# === Crypto Arb ===
_crypto = _yaml.get("crypto_arb", {})
CRYPTO_ARB_MODE = _crypto.get("mode", "alert")
CRYPTO_ARB_COINS = _crypto.get("coins", [
    {"symbol": "BTC", "binance_pair": "btcusdt"},
    {"symbol": "ETH", "binance_pair": "ethusdt"},
    {"symbol": "SOL", "binance_pair": "solusdt"},
    {"symbol": "XRP", "binance_pair": "xrpusdt"},
])
CRYPTO_ARB_MIN_MOVE_PCT = _crypto.get("min_price_move_pct", 0.15)
CRYPTO_ARB_MAX_POLY_ODDS = _crypto.get("max_poly_odds", 0.65)
CRYPTO_ARB_MIN_CONFIDENCE = _crypto.get("min_confidence_pct", 70)
CRYPTO_ARB_MIN_TIME_SEC = _crypto.get("min_time_remaining_sec", 120)
CRYPTO_ARB_MAX_TIME_SEC = _crypto.get("max_time_remaining_sec", 720)
CRYPTO_ARB_LOOKBACK_SEC = _crypto.get("lookback_seconds", 180)
CRYPTO_ARB_PAPER_BET = _crypto.get("paper_bet_size", 100)
CRYPTO_ARB_MAX_DAILY = _crypto.get("max_daily_signals", 500)
CRYPTO_ARB_TELEGRAM = _crypto.get("telegram_alerts", True)
# Estrategia: "divergence" (antigua) o "score" (nueva basada en price_to_beat)
CRYPTO_ARB_STRATEGY = _crypto.get("strategy", "score")
# Params estrategia score-based
CRYPTO_ARB_MIN_SCORE = _crypto.get("min_score", 0.40)
CRYPTO_ARB_ENTRY_MAX_TIME = _crypto.get("entry_max_time_sec", 180)
CRYPTO_ARB_MIN_DISTANCE_ATR = _crypto.get("min_distance_atr", 0.3)
CRYPTO_ARB_MIN_TREND_CONSISTENCY = _crypto.get("min_trend_consistency", 0.55)
# Params Sniper strategy (entrada rápida basada en movimiento del primer minuto)
_sniper_crypto = _crypto.get("sniper", {})
CRYPTO_SNIPER_MIN_MOVE_PCT = _sniper_crypto.get("min_move_pct", 0.03)
CRYPTO_SNIPER_ENTRY_DELAY_SEC = _sniper_crypto.get("entry_delay_sec", 55)
CRYPTO_SNIPER_ENTRY_MAX_SEC = _sniper_crypto.get("entry_max_sec", 150)
CRYPTO_SNIPER_MAX_BUY_PRICE = _sniper_crypto.get("max_buy_price", 0.60)
CRYPTO_SNIPER_INTERVALS = _sniper_crypto.get("intervals", [300, 900])  # 5m y 15m
# Params Early Entry strategy
_early = _crypto.get("early_entry", {})
FEATURE_EARLY_ENTRY = _features.get("early_entry", False)
EARLY_ENTRY_PRE_MONITOR_SEC = _early.get("pre_monitor_sec", 120)
EARLY_ENTRY_WINDOW_SEC = _early.get("entry_window_sec", 15)
EARLY_ENTRY_MIN_MOMENTUM_PCT = _early.get("min_momentum_pct", 0.03)
EARLY_ENTRY_BET_SIZE = _early.get("bet_size", 3)

# === Weather Arb ===
_weather = _yaml.get("weather_arb", {})
FEATURE_WEATHER_ARB = _features.get("weather_arb", False)
WEATHER_ARB_MIN_EDGE = _weather.get("min_edge_pct", 8.0)
WEATHER_ARB_MIN_CONFIDENCE = _weather.get("min_confidence_pct", 50.0)
WEATHER_ARB_MAX_POLY_ODDS = _weather.get("max_poly_odds", 0.85)
WEATHER_ARB_SCAN_INTERVAL = _weather.get("scan_interval_sec", 300)
WEATHER_ARB_FORECAST_REFRESH = _weather.get("forecast_refresh_sec", 1800)
WEATHER_ARB_PAPER_BET = _weather.get("paper_bet_size", 50)
WEATHER_ARB_TELEGRAM = _weather.get("telegram_alerts", True)
WEATHER_ARB_CITIES = _weather.get("cities", None)  # None = todas

# -- Weather Multi-Source --
_weather_multi = _weather.get("multi_source", {})
WEATHER_MULTI_SOURCE_ENABLED = _weather_multi.get("enabled", False)
WEATHER_MULTI_SOURCE_REFRESH = _weather_multi.get("refresh_sec", 3600)
WEATHER_MULTI_MIN_SOURCES = _weather_multi.get("min_sources", 2)
WEATHERAPI_KEY = os.getenv("WEATHERAPI_KEY", "")
VISUAL_CROSSING_KEY = os.getenv("VISUAL_CROSSING_KEY", "")

# -- Weather Elimination Strategy --
_weather_elim = _weather.get("elimination", {})
WEATHER_ELIMINATION_ENABLED = _weather_elim.get("enabled", False)
WEATHER_ELIMINATION_MIN_PROFIT = _weather_elim.get("min_profit_pct", 2.0)
WEATHER_ELIMINATION_MAX_BET = _weather_elim.get("max_bet", 50)
WEATHER_ELIMINATION_REQUIRE_ZERO = _weather_elim.get("require_zero_members", True)

# -- Weather Early Detector --
_weather_early = _weather.get("early_detector", {})
WEATHER_EARLY_ENABLED = _weather_early.get("enabled", False)
WEATHER_EARLY_SCAN_INTERVAL = _weather_early.get("scan_interval_sec", 60)
WEATHER_EARLY_MIN_EDGE = _weather_early.get("min_edge_pct", 5.0)
WEATHER_EARLY_MIN_CONFIDENCE = _weather_early.get("min_confidence_pct", 40.0)
WEATHER_EARLY_ENTRY_WINDOW = _weather_early.get("entry_window_sec", 300)

# -- Weather Trailing Stop --
WEATHER_TRAILING_STOP_ENABLED = _weather.get("trailing_stop_enabled", False)
WEATHER_TRAILING_STOP_PCT = _weather.get("trailing_stop_pct", 15.0)

# -- Weather Wallet Sharing (usar misma wallet que crypto) --
WEATHER_USE_CRYPTO_WALLET = _weather.get("use_crypto_wallet", False)

# === Nuevas Features v8.0 ===

# -- Correlation Filter (Alert Trading) --
_corr = _yaml.get("correlation_filter", {})
FEATURE_CORRELATION_FILTER = True  # Siempre activo — gestión de riesgo básica
CORRELATION_MIN_OVERLAP = _corr.get("min_overlap_pct", 70)  # % mínimo de correlación
CORRELATION_MAX_EXPOSURE = _corr.get("max_exposure_pct", 25)  # máx % del bankroll en mercados correlacionados

# -- Multi-Timeframe (Crypto Arb) --
_mtf = _yaml.get("multi_timeframe", {})
FEATURE_MULTI_TIMEFRAME = _features.get("multi_timeframe", True)
MTF_WINDOWS = _mtf.get("windows_sec", [30, 60, 180, 300])  # ventanas en segundos
MTF_MIN_AGREEMENT = _mtf.get("min_agreement", 3)  # mín timeframes que deben coincidir
MTF_BOOST_POINTS = _mtf.get("boost_points", 15)  # puntos extra si todos coinciden

# -- VWAP Indicator (Crypto Arb) --
_vwap = _yaml.get("vwap", {})
FEATURE_VWAP = _features.get("vwap", False)
VWAP_LOOKBACK_SEC = _vwap.get("lookback_sec", 300)
VWAP_MIN_DEVIATION_PCT = _vwap.get("min_deviation_pct", 0.05)  # desviación mín para señal

# -- RSI Indicator (Crypto Arb) --
_rsi = _yaml.get("rsi", {})
FEATURE_RSI = _features.get("rsi", False)
RSI_PERIOD = _rsi.get("period", 14)
RSI_CANDLE_SEC = _rsi.get("candle_sec", 10)
RSI_OVERBOUGHT = _rsi.get("overbought", 70)
RSI_OVERSOLD = _rsi.get("oversold", 30)
RSI_BOOST_PCT = _rsi.get("boost_pct", 10)  # % boost al score si RSI confirma dirección

# -- MACD Indicator (Crypto Arb) --
_macd = _yaml.get("macd", {})
FEATURE_MACD = _features.get("macd", True)
MACD_FAST = _macd.get("fast", 12)
MACD_SLOW = _macd.get("slow", 26)
MACD_SIGNAL = _macd.get("signal", 9)
MACD_CANDLE_SEC = _macd.get("candle_sec", 10)
MACD_BOOST_PCT = _macd.get("boost_pct", 10)  # % boost al score si MACD confirma

# -- Stop-Loss / Take-Profit para Crypto Autotrader --
_at_risk = _yaml.get("at_risk_management", {})
FEATURE_AT_STOP_LOSS = _features.get("at_stop_loss", False)
AT_STOP_LOSS_PCT = _at_risk.get("stop_loss_pct", 25)  # % pérdida para vender
AT_TAKE_PROFIT_PCT = _at_risk.get("take_profit_pct", 30)  # % ganancia para vender
AT_MAX_HOLDING_SEC = _at_risk.get("max_holding_sec", 1800)  # máx 30 min holding
AT_TRAILING_STOP_ENABLED = _at_risk.get("trailing_stop", False)
AT_TRAILING_STOP_PCT = _at_risk.get("trailing_stop_pct", 15)
AT_SLIPPAGE_MAX_PCT = _at_risk.get("slippage_max_pct", 3.0)  # máx spread para entrar

# -- Maker Orders (Crypto Arb) --
_maker = _yaml.get("maker_orders", {})
AT_MAKER_SPREAD_OFFSET = _maker.get("spread_offset", 0.02)  # centavos por debajo del ask
AT_MAKER_MAX_OPEN_ORDERS = _maker.get("max_open_orders", 10)  # max órdenes maker simultáneas
AT_MAKER_REQUOTE_THRESHOLD = _maker.get("requote_threshold", 0.03)  # re-quotear si precio cambia >X
AT_MAKER_FILL_TIMEOUT_SEC = _maker.get("fill_timeout_sec", 120)  # cancelar si no se llena en N seg
AT_HYBRID_SCORE_THRESHOLD = _maker.get("hybrid_score_threshold", 0.50)  # score mín para sesgo direccional

# -- Paper Trading (Crypto Arb) --
_paper = _yaml.get("paper_trading", {})
FEATURE_PAPER_TRADING = _features.get("paper_trading", False)
PAPER_BET_SIZE = _paper.get("bet_size", 10)  # bet size simulado
PAPER_SPREAD_OFFSET = _paper.get("spread_offset", 0.02)  # spread offset simulado
PAPER_INITIAL_CAPITAL = _paper.get("initial_capital", 500)  # capital inicial simulado
PAPER_MODE = _paper.get("mode", "maker")  # "maker" o "hybrid"
PAPER_FILL_TIMEOUT_SEC = _paper.get("fill_timeout_sec", 120)  # timeout para simular fill
PAPER_REBATE_RATE = _paper.get("rebate_rate", 0.005)  # rebate estimado (0.5% del volumen)
PAPER_TAKER_FEE_RATE = _paper.get("taker_fee_rate", 0.0156)  # taker fee para comparativa

# -- Order Book Depth Crypto (Crypto Arb) --
_obd_crypto = _yaml.get("orderbook_crypto", {})
FEATURE_ORDERBOOK_CRYPTO = _features.get("orderbook_crypto", True)
ORDERBOOK_CRYPTO_MIN_DEPTH = _obd_crypto.get("min_depth_usd", 500)  # liquidez mín en USD
ORDERBOOK_CRYPTO_MAX_IMPACT_PCT = _obd_crypto.get("max_impact_pct", 2.0)  # máx impacto en precio

# -- Hedging Automático (Crypto Arb) --
_hedge = _yaml.get("hedging", {})
FEATURE_HEDGING = _features.get("hedging", False)
HEDGE_TRIGGER_LOSS_PCT = _hedge.get("trigger_loss_pct", 30)  # % de pérdida para activar hedge
HEDGE_SIZE_PCT = _hedge.get("size_pct", 50)  # % del tamaño original para el hedge

# -- News Catalyst Signal #19 (Detection) --
_news = _yaml.get("news_catalyst", {})
FEATURE_NEWS_CATALYST = _features.get("news_catalyst", False)
NEWS_CATALYST_POINTS = _news.get("points", 3)
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")
NEWS_MIN_MENTIONS = _news.get("min_mentions", 3)  # menciones mín en últimas horas
NEWS_LOOKBACK_HOURS = _news.get("lookback_hours", 6)

# -- ML Scoring (Detection) --
_ml = _yaml.get("ml_scoring", {})
FEATURE_ML_SCORING = _features.get("ml_scoring", False)
ML_MIN_TRAINING_SAMPLES = _ml.get("min_training_samples", 100)
ML_RETRAIN_HOURS = _ml.get("retrain_hours", 24)
ML_WEIGHT = _ml.get("weight", 0.5)  # peso del ML vs scoring tradicional (0=solo trad, 1=solo ML)

# -- Heatmap de Mercados (Dashboard) --
FEATURE_HEATMAP = True  # Siempre activo — solo vista de datos

# -- Trade Journal (Dashboard) --
FEATURE_TRADE_JOURNAL = True  # Siempre activo — solo UI para notas

# -- Push Notifications (Dashboard) --
FEATURE_PUSH_NOTIFICATIONS = True  # Siempre activo — solo muestra notifs
PUSH_MIN_SCORE = int(os.getenv("PUSH_MIN_SCORE", "7"))

# -- WebSocket Polymarket (Infrastructure) --
FEATURE_WEBSOCKET = _features.get("websocket", True)
WS_POLYMARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_RECONNECT_DELAY = 5  # segundos entre reconexiones
WS_TRADE_BUFFER_SEC = 2  # buffer para deduplicar trades WS vs polling

# -- Queue System (Infrastructure) --
FEATURE_QUEUE = _features.get("queue_system", False)
QUEUE_MAX_SIZE = 1000
QUEUE_WORKERS = 2

# -- Rate Limiting (Infrastructure) --
FEATURE_RATE_LIMITING = True  # Siempre activo — protección contra bans de API
RATE_LIMIT_MAX_PER_MIN = 60  # requests máx por minuto a Polymarket
RATE_LIMIT_BACKOFF_BASE = 1.5  # base exponencial para backoff

# -- Bankroll Tracking (Risk) --
FEATURE_BANKROLL = True  # Siempre activo — solo trackea balance, sin riesgo
BANKROLL_WALLET_ADDRESS = os.getenv("BANKROLL_WALLET_ADDRESS", "")
BANKROLL_INITIAL = float(os.getenv("BANKROLL_INITIAL", "1000"))
BANKROLL_MAX_SINGLE_BET_PCT = 5.0  # máx % del bankroll en un solo trade

# -- Market Making Bot (Bilateral) --
FEATURE_MARKET_MAKING = _features.get("market_making", False)
_mm = _yaml.get("market_making", {})
MM_BET_SIZE = _mm.get("bet_size", 5.0)          # USD por lado (Up + Down)
MM_SPREAD_TARGET = _mm.get("spread_target", 0.03)  # spread objetivo en $ (optimizado)
MM_MAX_INVENTORY = _mm.get("max_inventory", 50.0)  # máx exposure neto por mercado
MM_MAX_MARKETS = _mm.get("max_markets", 15)       # máx mercados simultáneos (optimizado)
MM_REQUOTE_SEC = _mm.get("requote_sec", 5)        # re-quotear cada N seg (optimizado)
MM_REQUOTE_THRESHOLD = _mm.get("requote_threshold", 0.02)  # re-quotear si precio cambia > X
MM_FILL_TIMEOUT_SEC = _mm.get("fill_timeout_sec", 60)      # cancelar si no llena (optimizado)
MM_BIAS_ENABLED = _mm.get("bias_enabled", True)   # sesgo direccional
MM_BIAS_STRENGTH = _mm.get("bias_strength", 0.01) # fuerza del sesgo en $ (optimizado)
MM_PAPER_MODE = _mm.get("paper_mode", True)        # empezar en paper
MM_MAX_DAILY_LOSS = _mm.get("max_daily_loss", 20.0)  # pausar si pierde > X
MM_MIN_TIME_REMAINING = _mm.get("min_time_remaining_sec", 180)  # no quotear < 3 min
MM_REBATE_RATE = _mm.get("rebate_rate", 0.005)    # rebate estimado 0.5%
# Compat legacy
MM_SPREAD_PCT = 3.0
MM_ORDER_SIZE = MM_BET_SIZE
MM_REFRESH_SEC = MM_REQUOTE_SEC
MM_MIN_LIQUIDITY = 10000

# -- Event-Driven Bot --
FEATURE_EVENT_DRIVEN = _features.get("event_driven", False)
ED_CHECK_INTERVAL = 60  # segundos entre checks
ED_MIN_EDGE_PCT = 5.0  # ventaja mínima en % para tradear
ED_SOURCES = ["uma_oracle", "crypto_prices"]

# -- Spike Detection Bot --
FEATURE_SPIKE_DETECTION = _features.get("spike_detection", False)
SPIKE_MIN_MOVE_PCT = 10.0  # movimiento mínimo en %
SPIKE_LOOKBACK_MIN = 5  # ventana de detección en minutos
SPIKE_STRATEGY = "mean_reversion"  # "mean_reversion" o "momentum"
SPIKE_BET_SIZE = 10.0
SPIKE_MAX_DAILY = 20

# -- Cross-Platform Arb --
FEATURE_CROSS_PLATFORM = _features.get("cross_platform_arb", False)
CROSS_PLATFORM_MIN_EDGE = 3.0  # ventaja mínima en %
CROSS_PLATFORM_SOURCES = ["kalshi", "predictit", "manifold"]

# === v10.0: Binary Complement Arbitrage ===
_comp_arb = _yaml.get("complement_arb", {})
FEATURE_COMPLEMENT_ARB = _features.get("complement_arb", False)
COMPLEMENT_ARB_MIN_EDGE = _comp_arb.get("min_edge_pct", 1.0)
COMPLEMENT_ARB_SCAN_INTERVAL = _comp_arb.get("scan_interval_sec", 60)
COMPLEMENT_ARB_MAX_MARKETS = _comp_arb.get("max_markets", 100)

# === v10.0: Alert Backtester ===
FEATURE_BACKTESTER = True  # Siempre disponible — solo analiza datos

# === v10.0: WebSocket Real-Time Detection ===
FEATURE_WS_REALTIME = _features.get("ws_realtime", False)
WS_REALTIME_MIN_SIZE = _comp_arb.get("ws_min_size_usd", 100)

# === Dashboard ===
DASHBOARD_PORT = int(os.getenv("PORT", 8080))


def restore_from_db(saved: dict):
    """Restaurar TODAS las configs desde DB al iniciar el bot.
    Esto asegura que los cambios hechos desde el dashboard persisten
    entre reinicios de Railway.
    """
    import src.config as cfg
    _int = lambda k, default: int(saved[k]) if k in saved else default
    _float = lambda k, default: float(saved[k]) if k in saved else default
    _bool = lambda k, default: saved[k].lower() in ("true", "1", "yes") if k in saved else default

    # Detection
    cfg.MIN_SIZE_USD = _int("min_size_usd", cfg.MIN_SIZE_USD)
    cfg.LARGE_SIZE_USD = _int("large_size_usd", cfg.LARGE_SIZE_USD)
    cfg.ALERT_THRESHOLD = _int("alert_threshold", cfg.ALERT_THRESHOLD)
    cfg.POLL_INTERVAL = _int("poll_interval", cfg.POLL_INTERVAL)
    cfg.MAX_MARKETS = _int("max_markets", cfg.MAX_MARKETS)

    # Scoring señales 1-7
    cfg.FRESH_WALLET_POINTS = _int("fresh_wallet_points", cfg.FRESH_WALLET_POINTS)
    cfg.LARGE_SIZE_POINTS = _int("large_size_points", cfg.LARGE_SIZE_POINTS)
    cfg.MARKET_ANOMALY_POINTS = _int("market_anomaly_points", cfg.MARKET_ANOMALY_POINTS)
    cfg.WALLET_SHIFT_POINTS = _int("wallet_shift_points", cfg.WALLET_SHIFT_POINTS)
    cfg.CONCENTRATION_POINTS = _int("concentration_points", cfg.CONCENTRATION_POINTS)
    cfg.TIME_PROXIMITY_POINTS = _int("time_proximity_points", cfg.TIME_PROXIMITY_POINTS)
    cfg.CLUSTER_POINTS = _int("cluster_points", cfg.CLUSTER_POINTS)

    # Scoring señales 8-14
    cfg.HIT_RATE_MIN_RESOLVED = _int("hit_rate_min_resolved", cfg.HIT_RATE_MIN_RESOLVED)
    cfg.HIT_RATE_MIN_PCT = _int("hit_rate_min_pct", cfg.HIT_RATE_MIN_PCT)
    cfg.HIT_RATE_POINTS = _int("hit_rate_points", cfg.HIT_RATE_POINTS)
    cfg.CONTRARIAN_POINTS = _int("contrarian_points", cfg.CONTRARIAN_POINTS)
    cfg.ACCUMULATION_POINTS = _int("accumulation_points", cfg.ACCUMULATION_POINTS)
    cfg.PROVEN_WINNER_MIN_RESOLVED = _int("proven_winner_min_resolved", cfg.PROVEN_WINNER_MIN_RESOLVED)
    cfg.PROVEN_WINNER_MIN_PCT = _int("proven_winner_min_pct", cfg.PROVEN_WINNER_MIN_PCT)
    cfg.PROVEN_WINNER_POINTS = _int("proven_winner_points", cfg.PROVEN_WINNER_POINTS)
    cfg.MULTI_SMART_POINTS = _int("multi_smart_points", cfg.MULTI_SMART_POINTS)
    cfg.LATE_INSIDER_POINTS = _int("late_insider_points", cfg.LATE_INSIDER_POINTS)
    cfg.EXIT_ALERT_MIN_RESOLVED = _int("exit_alert_min_resolved", cfg.EXIT_ALERT_MIN_RESOLVED)
    cfg.EXIT_ALERT_MIN_PCT = _int("exit_alert_min_pct", cfg.EXIT_ALERT_MIN_PCT)
    cfg.EXIT_ALERT_POINTS = _int("exit_alert_points", cfg.EXIT_ALERT_POINTS)
    cfg.CROSS_BASKET_EXTRA_POINTS = _int("cross_basket_extra_points", cfg.CROSS_BASKET_EXTRA_POINTS)

    # Módulos
    cfg.ORDERBOOK_DEPTH_POINTS = _int("orderbook_depth_points", cfg.ORDERBOOK_DEPTH_POINTS)
    cfg.NICHE_MARKET_POINTS = _int("niche_market_points", cfg.NICHE_MARKET_POINTS)
    cfg.ORDERBOOK_MIN_DEPTH_PCT = _float("ob_min_depth_pct", cfg.ORDERBOOK_MIN_DEPTH_PCT)
    cfg.NICHE_MAX_LIQUIDITY = _int("niche_max_liquidity", cfg.NICHE_MAX_LIQUIDITY)
    cfg.NICHE_SCORE_MULTIPLIER = _float("niche_score_multiplier", cfg.NICHE_SCORE_MULTIPLIER)

    # Wallet Baskets
    cfg.BASKET_MIN_WALLET_TRADES = _int("basket_min_trades", cfg.BASKET_MIN_WALLET_TRADES)
    cfg.BASKET_CATEGORY_SHIFT_THRESHOLD = _float("basket_shift_threshold", cfg.BASKET_CATEGORY_SHIFT_THRESHOLD)
    cfg.BASKET_POINTS = _int("basket_points", cfg.BASKET_POINTS)
    cfg.BASKET_CROSS_MIN = _int("basket_cross_min", cfg.BASKET_CROSS_MIN)

    # Whale Tracker
    cfg.WHALE_TRACKER_MIN_SIZE = _int("whale_tracker_min_size", cfg.WHALE_TRACKER_MIN_SIZE)
    cfg.WHALE_TRACKER_ENABLED = _bool("whale_tracker_enabled", cfg.WHALE_TRACKER_ENABLED)

    # Sniper DBSCAN
    cfg.SNIPER_TIME_WINDOW_SEC = _int("sniper_time_window", cfg.SNIPER_TIME_WINDOW_SEC)
    cfg.SNIPER_MIN_CLUSTER_SIZE = _int("sniper_min_cluster", cfg.SNIPER_MIN_CLUSTER_SIZE)
    cfg.SNIPER_MIN_TRADE_SIZE = _int("sniper_min_size", cfg.SNIPER_MIN_TRADE_SIZE)
    cfg.SNIPER_POINTS = _int("sniper_points", cfg.SNIPER_POINTS)

    # Smart Money
    cfg.SMART_WALLET_MIN_WINRATE = _float("smart_wallet_min_winrate", cfg.SMART_WALLET_MIN_WINRATE)
    cfg.COOLDOWN_HOURS = _int("cooldown_hours", cfg.COOLDOWN_HOURS)

    # Feature flags
    cfg.FEATURE_ORDERBOOK_DEPTH = _bool("feature_orderbook_depth", cfg.FEATURE_ORDERBOOK_DEPTH)
    cfg.FEATURE_MARKET_CLASSIFICATION = _bool("feature_market_classification", cfg.FEATURE_MARKET_CLASSIFICATION)
    cfg.FEATURE_WALLET_BASKETS = _bool("feature_wallet_baskets", cfg.FEATURE_WALLET_BASKETS)
    cfg.FEATURE_SNIPER_DBSCAN = _bool("feature_sniper_dbscan", cfg.FEATURE_SNIPER_DBSCAN)
    cfg.FEATURE_CRYPTO_ARB = _bool("feature_crypto_arb", cfg.FEATURE_CRYPTO_ARB)

    # Crypto Arb
    cfg.CRYPTO_ARB_MIN_MOVE_PCT = _float("crypto_min_move_pct", cfg.CRYPTO_ARB_MIN_MOVE_PCT)
    cfg.CRYPTO_ARB_MAX_POLY_ODDS = _float("crypto_max_poly_odds", cfg.CRYPTO_ARB_MAX_POLY_ODDS)
    cfg.CRYPTO_ARB_MIN_CONFIDENCE = _float("crypto_min_confidence", cfg.CRYPTO_ARB_MIN_CONFIDENCE)
    cfg.CRYPTO_ARB_PAPER_BET = _float("crypto_paper_bet", cfg.CRYPTO_ARB_PAPER_BET)
    cfg.CRYPTO_ARB_MAX_DAILY = _int("crypto_max_daily", cfg.CRYPTO_ARB_MAX_DAILY)
    cfg.CRYPTO_ARB_TELEGRAM = _bool("crypto_telegram", cfg.CRYPTO_ARB_TELEGRAM)
    # Estrategia
    if "crypto_strategy" in saved:
        cfg.CRYPTO_ARB_STRATEGY = saved["crypto_strategy"]
    cfg.CRYPTO_ARB_MIN_SCORE = _float("crypto_min_score", cfg.CRYPTO_ARB_MIN_SCORE)
    cfg.CRYPTO_ARB_ENTRY_MAX_TIME = _int("crypto_entry_max_time", cfg.CRYPTO_ARB_ENTRY_MAX_TIME)
    cfg.CRYPTO_ARB_MIN_DISTANCE_ATR = _float("crypto_min_distance_atr", cfg.CRYPTO_ARB_MIN_DISTANCE_ATR)
    cfg.CRYPTO_ARB_MIN_TREND_CONSISTENCY = _float("crypto_min_trend_consistency", cfg.CRYPTO_ARB_MIN_TREND_CONSISTENCY)
    # Sniper params
    cfg.CRYPTO_SNIPER_MIN_MOVE_PCT = _float("crypto_sniper_min_move", cfg.CRYPTO_SNIPER_MIN_MOVE_PCT)
    cfg.CRYPTO_SNIPER_ENTRY_DELAY_SEC = _int("crypto_sniper_entry_delay", cfg.CRYPTO_SNIPER_ENTRY_DELAY_SEC)
    cfg.CRYPTO_SNIPER_ENTRY_MAX_SEC = _int("crypto_sniper_entry_max", cfg.CRYPTO_SNIPER_ENTRY_MAX_SEC)
    cfg.CRYPTO_SNIPER_MAX_BUY_PRICE = _float("crypto_sniper_max_buy_price", cfg.CRYPTO_SNIPER_MAX_BUY_PRICE)

    # Weather Arb
    cfg.FEATURE_WEATHER_ARB = _bool("feature_weather_arb", cfg.FEATURE_WEATHER_ARB)
    cfg.WEATHER_ARB_MIN_EDGE = _float("weather_min_edge", cfg.WEATHER_ARB_MIN_EDGE)
    cfg.WEATHER_ARB_MIN_CONFIDENCE = _float("weather_min_confidence", cfg.WEATHER_ARB_MIN_CONFIDENCE)
    cfg.WEATHER_ARB_MAX_POLY_ODDS = _float("weather_max_poly_odds", cfg.WEATHER_ARB_MAX_POLY_ODDS)
    cfg.WEATHER_ARB_SCAN_INTERVAL = _int("weather_scan_interval", cfg.WEATHER_ARB_SCAN_INTERVAL)
    cfg.WEATHER_ARB_FORECAST_REFRESH = _int("weather_forecast_refresh", cfg.WEATHER_ARB_FORECAST_REFRESH)
    cfg.WEATHER_ARB_PAPER_BET = _float("weather_paper_bet", cfg.WEATHER_ARB_PAPER_BET)
    cfg.WEATHER_ARB_TELEGRAM = _bool("weather_telegram", cfg.WEATHER_ARB_TELEGRAM)

    # Weather nuevos
    cfg.WEATHER_MULTI_SOURCE_ENABLED = _bool("weather_multi_source_enabled", cfg.WEATHER_MULTI_SOURCE_ENABLED)
    cfg.WEATHER_MULTI_SOURCE_REFRESH = _int("weather_multi_source_refresh", cfg.WEATHER_MULTI_SOURCE_REFRESH)
    cfg.WEATHER_MULTI_MIN_SOURCES = _int("weather_multi_min_sources", cfg.WEATHER_MULTI_MIN_SOURCES)
    cfg.WEATHER_ELIMINATION_ENABLED = _bool("weather_elimination_enabled", cfg.WEATHER_ELIMINATION_ENABLED)
    cfg.WEATHER_ELIMINATION_MIN_PROFIT = _float("weather_elimination_min_profit", cfg.WEATHER_ELIMINATION_MIN_PROFIT)
    cfg.WEATHER_ELIMINATION_MAX_BET = _float("weather_elimination_max_bet", cfg.WEATHER_ELIMINATION_MAX_BET)
    cfg.WEATHER_ELIMINATION_REQUIRE_ZERO = _bool("weather_elimination_require_zero", cfg.WEATHER_ELIMINATION_REQUIRE_ZERO)
    cfg.WEATHER_EARLY_ENABLED = _bool("weather_early_enabled", cfg.WEATHER_EARLY_ENABLED)
    cfg.WEATHER_EARLY_SCAN_INTERVAL = _int("weather_early_scan_interval", cfg.WEATHER_EARLY_SCAN_INTERVAL)
    cfg.WEATHER_EARLY_MIN_EDGE = _float("weather_early_min_edge", cfg.WEATHER_EARLY_MIN_EDGE)
    cfg.WEATHER_EARLY_MIN_CONFIDENCE = _float("weather_early_min_confidence", cfg.WEATHER_EARLY_MIN_CONFIDENCE)
    cfg.WEATHER_EARLY_ENTRY_WINDOW = _int("weather_early_entry_window", cfg.WEATHER_EARLY_ENTRY_WINDOW)
    cfg.WEATHER_TRAILING_STOP_ENABLED = _bool("weather_trailing_stop_enabled", cfg.WEATHER_TRAILING_STOP_ENABLED)
    cfg.WEATHER_TRAILING_STOP_PCT = _float("weather_trailing_stop_pct", cfg.WEATHER_TRAILING_STOP_PCT)
    cfg.WEATHER_USE_CRYPTO_WALLET = _bool("weather_use_crypto_wallet", cfg.WEATHER_USE_CRYPTO_WALLET)

    # === Telegram (desde DB) ===
    if "telegram_bot_token" in saved and saved["telegram_bot_token"]:
        cfg.TELEGRAM_BOT_TOKEN = saved["telegram_bot_token"]
    if "telegram_chat_ids" in saved and saved["telegram_chat_ids"]:
        cfg.TELEGRAM_CHAT_IDS = saved["telegram_chat_ids"]

    # === Features v8.0 ===
    # FEATURE_CORRELATION_FILTER = siempre True (no toggle)
    cfg.CORRELATION_MIN_OVERLAP = _int("correlation_min_overlap", cfg.CORRELATION_MIN_OVERLAP)
    cfg.CORRELATION_MAX_EXPOSURE = _int("correlation_max_exposure", cfg.CORRELATION_MAX_EXPOSURE)
    cfg.FEATURE_MULTI_TIMEFRAME = _bool("feature_multi_timeframe", cfg.FEATURE_MULTI_TIMEFRAME)
    cfg.MTF_MIN_AGREEMENT = _int("mtf_min_agreement", cfg.MTF_MIN_AGREEMENT)
    cfg.MTF_BOOST_POINTS = _int("mtf_boost_points", cfg.MTF_BOOST_POINTS)
    cfg.FEATURE_VWAP = _bool("feature_vwap", cfg.FEATURE_VWAP)
    cfg.VWAP_LOOKBACK_SEC = _int("vwap_lookback_sec", cfg.VWAP_LOOKBACK_SEC)
    cfg.VWAP_MIN_DEVIATION_PCT = _float("vwap_min_deviation_pct", cfg.VWAP_MIN_DEVIATION_PCT)
    cfg.FEATURE_ORDERBOOK_CRYPTO = _bool("feature_orderbook_crypto", cfg.FEATURE_ORDERBOOK_CRYPTO)
    cfg.ORDERBOOK_CRYPTO_MIN_DEPTH = _int("orderbook_crypto_min_depth", cfg.ORDERBOOK_CRYPTO_MIN_DEPTH)
    cfg.FEATURE_HEDGING = _bool("feature_hedging", cfg.FEATURE_HEDGING)
    cfg.HEDGE_TRIGGER_LOSS_PCT = _int("hedge_trigger_loss_pct", cfg.HEDGE_TRIGGER_LOSS_PCT)
    cfg.HEDGE_SIZE_PCT = _int("hedge_size_pct", cfg.HEDGE_SIZE_PCT)
    cfg.FEATURE_NEWS_CATALYST = _bool("feature_news_catalyst", cfg.FEATURE_NEWS_CATALYST)
    cfg.NEWS_CATALYST_POINTS = _int("news_catalyst_points", cfg.NEWS_CATALYST_POINTS)
    cfg.NEWS_MIN_MENTIONS = _int("news_min_mentions", cfg.NEWS_MIN_MENTIONS)
    cfg.FEATURE_ML_SCORING = _bool("feature_ml_scoring", cfg.FEATURE_ML_SCORING)
    cfg.ML_WEIGHT = _float("ml_weight", cfg.ML_WEIGHT)
    cfg.ML_RETRAIN_HOURS = _int("ml_retrain_hours", cfg.ML_RETRAIN_HOURS)
    # FEATURE_HEATMAP, TRADE_JOURNAL, PUSH_NOTIFICATIONS = siempre True (no toggle)
    cfg.PUSH_MIN_SCORE = _int("push_min_score", cfg.PUSH_MIN_SCORE)
    cfg.FEATURE_WEBSOCKET = _bool("feature_websocket", cfg.FEATURE_WEBSOCKET)
    cfg.FEATURE_QUEUE = _bool("feature_queue", cfg.FEATURE_QUEUE)
    # FEATURE_RATE_LIMITING, FEATURE_BANKROLL = siempre True (no toggle)
    cfg.RATE_LIMIT_MAX_PER_MIN = _int("rate_limit_max_per_min", cfg.RATE_LIMIT_MAX_PER_MIN)
    cfg.BANKROLL_INITIAL = _float("bankroll_initial", cfg.BANKROLL_INITIAL)
    cfg.BANKROLL_MAX_SINGLE_BET_PCT = _float("bankroll_max_bet_pct", cfg.BANKROLL_MAX_SINGLE_BET_PCT)
    cfg.FEATURE_MARKET_MAKING = _bool("feature_market_making", cfg.FEATURE_MARKET_MAKING)
    cfg.MM_BET_SIZE = _float("mm_bet_size", cfg.MM_BET_SIZE)
    cfg.MM_SPREAD_TARGET = _float("mm_spread_target", cfg.MM_SPREAD_TARGET)
    cfg.MM_MAX_INVENTORY = _float("mm_max_inventory", cfg.MM_MAX_INVENTORY)
    cfg.MM_MAX_MARKETS = _int("mm_max_markets", cfg.MM_MAX_MARKETS)
    cfg.MM_REQUOTE_SEC = _int("mm_requote_sec", cfg.MM_REQUOTE_SEC)
    cfg.MM_REQUOTE_THRESHOLD = _float("mm_requote_threshold", cfg.MM_REQUOTE_THRESHOLD)
    cfg.MM_FILL_TIMEOUT_SEC = _int("mm_fill_timeout_sec", cfg.MM_FILL_TIMEOUT_SEC)
    cfg.MM_BIAS_ENABLED = _bool("mm_bias_enabled", cfg.MM_BIAS_ENABLED)
    cfg.MM_BIAS_STRENGTH = _float("mm_bias_strength", cfg.MM_BIAS_STRENGTH)
    cfg.MM_PAPER_MODE = _bool("mm_paper_mode", cfg.MM_PAPER_MODE)
    cfg.MM_MAX_DAILY_LOSS = _float("mm_max_daily_loss", cfg.MM_MAX_DAILY_LOSS)
    cfg.MM_MIN_TIME_REMAINING = _int("mm_min_time_remaining_sec", cfg.MM_MIN_TIME_REMAINING)
    cfg.MM_REBATE_RATE = _float("mm_rebate_rate", cfg.MM_REBATE_RATE)
    # Compat legacy
    cfg.MM_SPREAD_PCT = _float("mm_spread_pct", cfg.MM_SPREAD_PCT)
    cfg.MM_ORDER_SIZE = cfg.MM_BET_SIZE
    cfg.FEATURE_EVENT_DRIVEN = _bool("feature_event_driven", cfg.FEATURE_EVENT_DRIVEN)
    cfg.ED_MIN_EDGE_PCT = _float("ed_min_edge_pct", cfg.ED_MIN_EDGE_PCT)
    cfg.FEATURE_SPIKE_DETECTION = _bool("feature_spike_detection", cfg.FEATURE_SPIKE_DETECTION)
    cfg.SPIKE_MIN_MOVE_PCT = _float("spike_min_move_pct", cfg.SPIKE_MIN_MOVE_PCT)
    cfg.SPIKE_BET_SIZE = _float("spike_bet_size", cfg.SPIKE_BET_SIZE)
    cfg.SPIKE_MAX_DAILY = _int("spike_max_daily", cfg.SPIKE_MAX_DAILY)
    cfg.FEATURE_CROSS_PLATFORM = _bool("feature_cross_platform", cfg.FEATURE_CROSS_PLATFORM)
    cfg.CROSS_PLATFORM_MIN_EDGE = _float("cross_platform_min_edge", cfg.CROSS_PLATFORM_MIN_EDGE)

    # === v9.0: RSI / MACD / SL-TP ===
    cfg.FEATURE_RSI = _bool("feature_rsi", cfg.FEATURE_RSI)
    cfg.RSI_PERIOD = _int("rsi_period", cfg.RSI_PERIOD)
    cfg.RSI_CANDLE_SEC = _int("rsi_candle_sec", cfg.RSI_CANDLE_SEC)
    cfg.RSI_OVERBOUGHT = _int("rsi_overbought", cfg.RSI_OVERBOUGHT)
    cfg.RSI_OVERSOLD = _int("rsi_oversold", cfg.RSI_OVERSOLD)
    cfg.RSI_BOOST_PCT = _int("rsi_boost_pct", cfg.RSI_BOOST_PCT)
    cfg.FEATURE_MACD = _bool("feature_macd", cfg.FEATURE_MACD)
    cfg.MACD_FAST = _int("macd_fast", cfg.MACD_FAST)
    cfg.MACD_SLOW = _int("macd_slow", cfg.MACD_SLOW)
    cfg.MACD_SIGNAL = _int("macd_signal", cfg.MACD_SIGNAL)
    cfg.MACD_CANDLE_SEC = _int("macd_candle_sec", cfg.MACD_CANDLE_SEC)
    cfg.MACD_BOOST_PCT = _int("macd_boost_pct", cfg.MACD_BOOST_PCT)
    cfg.FEATURE_AT_STOP_LOSS = _bool("feature_at_stop_loss", cfg.FEATURE_AT_STOP_LOSS)
    cfg.AT_STOP_LOSS_PCT = _float("at_stop_loss_pct", cfg.AT_STOP_LOSS_PCT)
    cfg.AT_TAKE_PROFIT_PCT = _float("at_take_profit_pct", cfg.AT_TAKE_PROFIT_PCT)
    cfg.AT_MAX_HOLDING_SEC = _int("at_max_holding_sec", cfg.AT_MAX_HOLDING_SEC)
    cfg.AT_TRAILING_STOP_ENABLED = _bool("at_trailing_stop_enabled", cfg.AT_TRAILING_STOP_ENABLED)
    cfg.AT_TRAILING_STOP_PCT = _float("at_trailing_stop_pct", cfg.AT_TRAILING_STOP_PCT)
    cfg.AT_SLIPPAGE_MAX_PCT = _float("at_slippage_max_pct", cfg.AT_SLIPPAGE_MAX_PCT)

    # === Maker Orders ===
    cfg.AT_MAKER_SPREAD_OFFSET = _float("at_maker_spread_offset", cfg.AT_MAKER_SPREAD_OFFSET)
    cfg.AT_MAKER_MAX_OPEN_ORDERS = _int("at_maker_max_open_orders", cfg.AT_MAKER_MAX_OPEN_ORDERS)
    cfg.AT_MAKER_REQUOTE_THRESHOLD = _float("at_maker_requote_threshold", cfg.AT_MAKER_REQUOTE_THRESHOLD)
    cfg.AT_MAKER_FILL_TIMEOUT_SEC = _int("at_maker_fill_timeout_sec", cfg.AT_MAKER_FILL_TIMEOUT_SEC)
    cfg.AT_HYBRID_SCORE_THRESHOLD = _float("at_hybrid_score_threshold", cfg.AT_HYBRID_SCORE_THRESHOLD)

    # === Paper Trading ===
    cfg.FEATURE_PAPER_TRADING = _bool("feature_paper_trading", cfg.FEATURE_PAPER_TRADING)
    cfg.PAPER_BET_SIZE = _float("paper_bet_size", cfg.PAPER_BET_SIZE)
    cfg.PAPER_SPREAD_OFFSET = _float("paper_spread_offset", cfg.PAPER_SPREAD_OFFSET)
    cfg.PAPER_INITIAL_CAPITAL = _float("paper_initial_capital", cfg.PAPER_INITIAL_CAPITAL)
    cfg.PAPER_MODE = saved.get("paper_mode", cfg.PAPER_MODE) if "paper_mode" in saved else cfg.PAPER_MODE

    # === v10.0 ===
    cfg.FEATURE_COMPLEMENT_ARB = _bool("feature_complement_arb", cfg.FEATURE_COMPLEMENT_ARB)
    cfg.COMPLEMENT_ARB_MIN_EDGE = _float("arb_complement_min_edge", cfg.COMPLEMENT_ARB_MIN_EDGE)
    cfg.COMPLEMENT_ARB_SCAN_INTERVAL = _int("arb_complement_scan_interval", cfg.COMPLEMENT_ARB_SCAN_INTERVAL)
    cfg.COMPLEMENT_ARB_MAX_MARKETS = _int("arb_complement_max_markets", cfg.COMPLEMENT_ARB_MAX_MARKETS)
    cfg.FEATURE_WS_REALTIME = _bool("feature_ws_realtime", cfg.FEATURE_WS_REALTIME)
