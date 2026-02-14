"""FastAPI routes para el dashboard y API."""
import asyncio
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pathlib import Path

from src import config

router = APIRouter()

DASHBOARD_HTML = Path(__file__).parent.parent / "dashboard" / "index.html"


# ── Dashboard ────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard():
    if DASHBOARD_HTML.exists():
        return HTMLResponse(DASHBOARD_HTML.read_text())
    return HTMLResponse("<h1>Dashboard loading...</h1>")


# ── Stats ────────────────────────────────────────────────────────────

@router.get("/api/stats")
async def get_stats(request: Request):
    db = request.app.state.db
    stats = await db.get_dashboard_stats()
    bot = request.app.state.bot
    if bot:
        uptime = datetime.now() - bot.start_time
        hours = int(uptime.total_seconds() // 3600)
        minutes = int((uptime.total_seconds() % 3600) // 60)
        stats["uptime"] = f"{hours}h {minutes}m"
        stats["trades_this_session"] = bot.trades_processed
        stats["alerts_this_session"] = bot.alerts_sent
        stats["watchlist_count"] = len(bot._watchlist)
    return stats


# ── Alerts ───────────────────────────────────────────────────────────

@router.get("/api/alerts")
async def get_alerts(request: Request, limit: int = 50):
    db = request.app.state.db
    return await db.get_recent_alerts(limit=limit)


@router.get("/api/alerts/market/{market_id:path}")
async def get_market_alerts(request: Request, market_id: str):
    db = request.app.state.db
    return await db.get_market_alerts(market_id)


# ── Wallets ──────────────────────────────────────────────────────────

@router.get("/api/wallets")
async def get_top_wallets(request: Request, limit: int = 20):
    db = request.app.state.db
    return await db.get_top_wallets(limit=limit)


@router.get("/api/wallets/{address}")
async def get_wallet_detail(request: Request, address: str):
    db = request.app.state.db
    detail = await db.get_wallet_detail(address)
    if not detail:
        return {"error": "Wallet no encontrada"}
    return detail


# ── Markets ──────────────────────────────────────────────────────────

@router.get("/api/markets")
async def get_markets(request: Request, limit: int = 50):
    db = request.app.state.db
    return await db.get_tracked_markets(limit=limit)


# ── Categories ───────────────────────────────────────────────────────

@router.get("/api/categories")
async def get_categories(request: Request):
    db = request.app.state.db
    return await db.get_category_distribution()


@router.get("/api/charts/category-alerts")
async def category_alerts(request: Request):
    db = request.app.state.db
    return await db.get_alert_category_distribution()


# ── Trades Feed ──────────────────────────────────────────────────────

@router.get("/api/trades/recent")
async def recent_trades(request: Request, limit: int = 100):
    db = request.app.state.db
    return await db.get_recent_trades_feed(limit=limit)


# ── Charts ───────────────────────────────────────────────────────────

@router.get("/api/charts/alerts-by-day")
async def alerts_by_day(request: Request, days: int = 30):
    db = request.app.state.db
    return await db.get_alerts_by_day(days=days)


@router.get("/api/charts/score-distribution")
async def score_distribution(request: Request):
    db = request.app.state.db
    return await db.get_score_distribution()


# ── Config ───────────────────────────────────────────────────────────

class ConfigUpdate(BaseModel):
    min_size_usd: int | None = None
    large_size_usd: int | None = None
    alert_threshold: int | None = None
    excluded_categories: str | None = None  # comma-separated


@router.get("/api/config")
async def get_config(request: Request):
    db = request.app.state.db
    saved = await db.get_config()
    return {
        "min_size_usd": int(saved.get("min_size_usd", config.MIN_SIZE_USD)),
        "large_size_usd": int(saved.get("large_size_usd", config.LARGE_SIZE_USD)),
        "alert_threshold": int(saved.get("alert_threshold", config.ALERT_THRESHOLD)),
        "excluded_categories": saved.get("excluded_categories", "sports,nba,nfl,nhl,mlb,mls,soccer,esports,crypto-prices"),
    }


@router.post("/api/config")
async def update_config(request: Request, body: ConfigUpdate):
    db = request.app.state.db
    data = {}
    if body.min_size_usd is not None:
        data["min_size_usd"] = str(body.min_size_usd)
        config.MIN_SIZE_USD = body.min_size_usd
    if body.large_size_usd is not None:
        data["large_size_usd"] = str(body.large_size_usd)
        config.LARGE_SIZE_USD = body.large_size_usd
    if body.alert_threshold is not None:
        data["alert_threshold"] = str(body.alert_threshold)
        config.ALERT_THRESHOLD = body.alert_threshold
    if body.excluded_categories is not None:
        data["excluded_categories"] = body.excluded_categories
    if data:
        await db.set_config_bulk(data)
    return {"status": "ok", "updated": list(data.keys())}


# ── Leaderboard ─────────────────────────────────────────────────────

@router.get("/api/leaderboard")
async def get_leaderboard(request: Request, limit: int = 30, sort: str = "pnl"):
    db = request.app.state.db
    return await db.get_leaderboard(limit=limit, sort_by=sort)


# ── Coordination ────────────────────────────────────────────────────

@router.get("/api/coordination")
async def get_coordination(request: Request, limit: int = 30):
    db = request.app.state.db
    return await db.get_all_coordination(limit=limit)


@router.get("/api/wallets/{address}/coordination")
async def get_wallet_coordination(request: Request, address: str):
    db = request.app.state.db
    return await db.get_wallet_coordination(address)


# ── Category Edge ───────────────────────────────────────────────────

@router.get("/api/category-edge")
async def category_edge(request: Request):
    db = request.app.state.db
    return await db.get_category_edge()


# ── Health ───────────────────────────────────────────────────────────

@router.get("/api/health")
async def health():
    return {"status": "ok"}


# ── Feature Flags ────────────────────────────────────────────

@router.get("/api/features")
async def get_features():
    return {
        "orderbook_depth": config.FEATURE_ORDERBOOK_DEPTH,
        "market_classification": config.FEATURE_MARKET_CLASSIFICATION,
        "wallet_baskets": config.FEATURE_WALLET_BASKETS,
        "sniper_dbscan": config.FEATURE_SNIPER_DBSCAN,
        "crypto_arb": config.FEATURE_CRYPTO_ARB,
    }


class FeaturesUpdate(BaseModel):
    orderbook_depth: bool | None = None
    market_classification: bool | None = None
    wallet_baskets: bool | None = None
    sniper_dbscan: bool | None = None
    crypto_arb: bool | None = None


@router.post("/api/features")
async def update_features(body: FeaturesUpdate):
    updated = {}
    if body.orderbook_depth is not None:
        config.FEATURE_ORDERBOOK_DEPTH = body.orderbook_depth
        updated["orderbook_depth"] = body.orderbook_depth
    if body.market_classification is not None:
        config.FEATURE_MARKET_CLASSIFICATION = body.market_classification
        updated["market_classification"] = body.market_classification
    if body.wallet_baskets is not None:
        config.FEATURE_WALLET_BASKETS = body.wallet_baskets
        updated["wallet_baskets"] = body.wallet_baskets
    if body.sniper_dbscan is not None:
        config.FEATURE_SNIPER_DBSCAN = body.sniper_dbscan
        updated["sniper_dbscan"] = body.sniper_dbscan
    if body.crypto_arb is not None:
        config.FEATURE_CRYPTO_ARB = body.crypto_arb
        updated["crypto_arb"] = body.crypto_arb
    return {"status": "ok", "updated": updated}


# ── Crypto Arb ───────────────────────────────────────────────

@router.get("/api/crypto-arb/stats")
async def crypto_arb_stats(request: Request):
    db = request.app.state.db
    db_stats = await db.get_crypto_arb_stats()
    bot = request.app.state.bot
    live_stats = {}
    if bot and hasattr(bot, "crypto_detector") and bot.crypto_detector:
        live_stats = bot.crypto_detector.get_stats()
    feed_status = {}
    if bot and hasattr(bot, "binance_feed") and bot.binance_feed:
        feed_status = bot.binance_feed.get_status()
    return {**db_stats, "live": live_stats, "feed": feed_status}


@router.get("/api/crypto-arb/signals")
async def crypto_arb_signals(request: Request, limit: int = 100, coin: str = None):
    db = request.app.state.db
    return await db.get_crypto_signals_history(limit=limit, coin=coin)


@router.get("/api/crypto-arb/live")
async def crypto_arb_live(request: Request):
    """Señales en vivo y mercados activos del detector."""
    bot = request.app.state.bot
    if not bot or not hasattr(bot, "crypto_detector") or not bot.crypto_detector:
        return {"signals": [], "markets": [], "enabled": False}
    return {
        "signals": bot.crypto_detector.get_recent_signals(50),
        "markets": bot.crypto_detector.get_active_markets(),
        "enabled": True,
    }


@router.get("/api/crypto-arb/prices")
async def crypto_arb_prices(request: Request):
    """Precios spot actuales de Binance."""
    bot = request.app.state.bot
    if not bot or not hasattr(bot, "binance_feed") or not bot.binance_feed:
        return {"prices": {}, "connected": False}
    feed = bot.binance_feed
    prices = {}
    for coin_cfg in config.CRYPTO_ARB_COINS:
        pair = coin_cfg["binance_pair"]
        symbol = coin_cfg["symbol"]
        momentum = feed.get_momentum(pair, config.CRYPTO_ARB_LOOKBACK_SEC)
        prices[symbol] = {
            "price": feed.get_price(pair),
            "momentum": momentum,
        }
    return {"prices": prices, "connected": feed.is_running}


class BacktestRequest(BaseModel):
    days: int = 7
    bet_size: float = 100
    max_odds: float = 0.65
    coins: list[str] | None = None


@router.post("/api/crypto-arb/backtest")
async def run_backtest(request: Request, body: BacktestRequest):
    """Ejecutar backtest en background."""
    bot = request.app.state.bot
    if not bot or not hasattr(bot, "backtester"):
        return {"error": "Backtester no disponible"}

    # Lanzar en background
    asyncio.create_task(
        bot.backtester.run_backtest(
            days=body.days,
            bet_size=body.bet_size,
            max_odds=body.max_odds,
            coins=body.coins,
        )
    )
    return {"status": "running", "params": {"days": body.days, "bet_size": body.bet_size, "max_odds": body.max_odds}}


@router.get("/api/crypto-arb/backtest")
async def get_backtest_result(request: Request):
    """Obtener último resultado de backtest."""
    bot = request.app.state.bot
    if not bot or not hasattr(bot, "backtester"):
        return {"error": "Backtester no disponible"}
    result = bot.backtester.get_last_result()
    if result:
        return result
    return {"status": "no_data", "message": "Ejecuta un backtest primero"}


# ── Crypto Arb Config ────────────────────────────────────────

@router.get("/api/crypto-arb/config")
async def get_crypto_config():
    return {
        "mode": config.CRYPTO_ARB_MODE,
        "coins": config.CRYPTO_ARB_COINS,
        "min_price_move_pct": config.CRYPTO_ARB_MIN_MOVE_PCT,
        "max_poly_odds": config.CRYPTO_ARB_MAX_POLY_ODDS,
        "min_confidence_pct": config.CRYPTO_ARB_MIN_CONFIDENCE,
        "min_time_remaining_sec": config.CRYPTO_ARB_MIN_TIME_SEC,
        "max_time_remaining_sec": config.CRYPTO_ARB_MAX_TIME_SEC,
        "lookback_seconds": config.CRYPTO_ARB_LOOKBACK_SEC,
        "paper_bet_size": config.CRYPTO_ARB_PAPER_BET,
        "max_daily_signals": config.CRYPTO_ARB_MAX_DAILY,
        "telegram_alerts": config.CRYPTO_ARB_TELEGRAM,
    }


class CryptoConfigUpdate(BaseModel):
    min_price_move_pct: float | None = None
    max_poly_odds: float | None = None
    min_confidence_pct: float | None = None
    paper_bet_size: float | None = None
    max_daily_signals: int | None = None
    telegram_alerts: bool | None = None


@router.post("/api/crypto-arb/config")
async def update_crypto_config(body: CryptoConfigUpdate):
    updated = {}
    if body.min_price_move_pct is not None:
        config.CRYPTO_ARB_MIN_MOVE_PCT = body.min_price_move_pct
        updated["min_price_move_pct"] = body.min_price_move_pct
    if body.max_poly_odds is not None:
        config.CRYPTO_ARB_MAX_POLY_ODDS = body.max_poly_odds
        updated["max_poly_odds"] = body.max_poly_odds
    if body.min_confidence_pct is not None:
        config.CRYPTO_ARB_MIN_CONFIDENCE = body.min_confidence_pct
        updated["min_confidence_pct"] = body.min_confidence_pct
    if body.paper_bet_size is not None:
        config.CRYPTO_ARB_PAPER_BET = body.paper_bet_size
        updated["paper_bet_size"] = body.paper_bet_size
    if body.max_daily_signals is not None:
        config.CRYPTO_ARB_MAX_DAILY = body.max_daily_signals
        updated["max_daily_signals"] = body.max_daily_signals
    if body.telegram_alerts is not None:
        config.CRYPTO_ARB_TELEGRAM = body.telegram_alerts
        updated["telegram_alerts"] = body.telegram_alerts
    return {"status": "ok", "updated": updated}
