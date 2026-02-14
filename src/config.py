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

# === Dashboard ===
DASHBOARD_PORT = int(os.getenv("PORT", 8080))
