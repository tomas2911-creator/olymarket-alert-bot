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
TIME_PROXIMITY_POINTS = 3  # Nuevo: proximidad al cierre
CLUSTER_POINTS = 3  # Nuevo: clustering de wallets
ALERT_THRESHOLD = int(os.getenv("ALERT_THRESHOLD", _scoring.get("alert_threshold", 5)))

# === Alerts ===
_alerts = _yaml.get("alerts", {})
COOLDOWN_HOURS = _alerts.get("cooldown_hours", 6)
MAX_ALERTS_PER_HOUR = _alerts.get("max_alerts_per_hour", 20)

# === Smart Money ===
SMART_MONEY_THRESHOLD = int(os.getenv("SMART_MONEY_THRESHOLD", 50))
COPY_TRADE_MIN_RESOLVED = int(os.getenv("COPY_TRADE_MIN_RESOLVED", 3))

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
CRYPTO_ARB_MAX_DAILY = _crypto.get("max_daily_signals", 50)
CRYPTO_ARB_TELEGRAM = _crypto.get("telegram_alerts", True)

# === Dashboard ===
DASHBOARD_PORT = int(os.getenv("PORT", 8080))
