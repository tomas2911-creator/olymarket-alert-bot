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
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", _polling.get("interval_seconds", 60)))
MAX_MARKETS = _polling.get("max_markets", 50)

# === Detection ===
_detection = _yaml.get("detection", {})
FRESH_WALLET_MAX_TRADES = _detection.get("fresh_wallet_max_trades", 5)
FRESH_WALLET_MAX_DAYS = _detection.get("fresh_wallet_max_days", 30)
MIN_SIZE_USD = int(os.getenv("MIN_SIZE_USD", _detection.get("min_size_usd", 50)))
LARGE_SIZE_USD = int(os.getenv("LARGE_SIZE_USD", _detection.get("large_size_usd", 500)))
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

# === Feature Flags ===
_features = _yaml.get("features", {})
FEATURE_ORDERBOOK_DEPTH = _features.get("orderbook_depth", False)
FEATURE_MARKET_CLASSIFICATION = _features.get("market_classification", False)
FEATURE_WALLET_BASKETS = _features.get("wallet_baskets", False)
FEATURE_SNIPER_DBSCAN = _features.get("sniper_dbscan", False)
FEATURE_CRYPTO_ARB = _features.get("crypto_arb", False)

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
