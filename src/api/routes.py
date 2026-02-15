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


@router.get("/api/debug")
async def get_debug(request: Request):
    bot = request.app.state.bot
    if not bot:
        return {"error": "bot not running"}
    return {
        "trades_processed": bot.trades_processed,
        "alerts_sent": bot.alerts_sent,
        "excluded_categories": list(bot._excluded_categories),
        "watchlist_size": len(bot._watchlist),
        "config": {
            "MIN_SIZE_USD": config.MIN_SIZE_USD,
            "LARGE_SIZE_USD": config.LARGE_SIZE_USD,
            "ALERT_THRESHOLD": config.ALERT_THRESHOLD,
            "LARGE_SIZE_POINTS": config.LARGE_SIZE_POINTS,
            "COOLDOWN_HOURS": config.COOLDOWN_HOURS,
        },
        "debug_counters": {k: v for k, v in bot._debug.items() if k != "last_scores"},
        "last_scored_trades": bot._debug.get("last_scores", []),
    }


@router.get("/api/debug/resolve-crypto")
async def debug_resolve_crypto(request: Request):
    """Diagnóstico de resolución crypto - muestra estado de señales pendientes."""
    db = request.app.state.db
    bot = request.app.state.bot
    try:
        unresolved = await db.get_unresolved_crypto_signals()
        results = []
        for sig in unresolved[:10]:
            cid = sig["condition_id"]
            created = sig.get("created_at", "")
            time_rem = sig.get("time_remaining_sec", 0)
            results.append({
                "cid": cid[:40],
                "coin": sig.get("coin"),
                "direction": sig.get("direction"),
                "created": str(created)[:19],
                "time_remaining_at_creation": time_rem,
            })
        # Contar resueltas
        all_sigs = await db.get_crypto_signals_history(limit=50)
        resolved = [s for s in all_sigs if s.get("resolved")]
        return {
            "unresolved_count": len(unresolved),
            "total_in_db": len(all_sigs),
            "resolved_count": len(resolved),
            "wins": sum(1 for s in resolved if s.get("paper_result") == "win"),
            "losses": sum(1 for s in resolved if s.get("paper_result") == "loss"),
            "resolution_method": "gamma_slug → clob → binance_auto",
            "unresolved_signals": results,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── Alerts ───────────────────────────────────────────────────────────

@router.get("/api/alerts")
async def get_alerts(request: Request, limit: int = 50):
    db = request.app.state.db
    return await db.get_recent_alerts(limit=limit)


@router.delete("/api/alerts")
async def delete_alerts(request: Request, older_than_hours: int = 24):
    """Borrar alertas más viejas que N horas."""
    db = request.app.state.db
    try:
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(hours=older_than_hours)
        async with db._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM alerts WHERE created_at < $1", cutoff
            )
            deleted = int(result.split(" ")[-1]) if result else 0
        return {"status": "ok", "deleted": deleted, "older_than_hours": older_than_hours}
    except Exception as e:
        return {"status": "error", "error": str(e)}


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
    excluded_categories: str | None = None
    poll_interval: int | None = None
    max_markets: int | None = None
    # Señales 1-7
    fresh_wallet_points: int | None = None
    large_size_points: int | None = None
    market_anomaly_points: int | None = None
    wallet_shift_points: int | None = None
    concentration_points: int | None = None
    time_proximity_points: int | None = None
    cluster_points: int | None = None
    # Señales 8-14
    hit_rate_min_resolved: int | None = None
    hit_rate_min_pct: int | None = None
    hit_rate_points: int | None = None
    contrarian_points: int | None = None
    accumulation_points: int | None = None
    proven_winner_min_resolved: int | None = None
    proven_winner_min_pct: int | None = None
    proven_winner_points: int | None = None
    multi_smart_points: int | None = None
    late_insider_points: int | None = None
    exit_alert_min_resolved: int | None = None
    exit_alert_min_pct: int | None = None
    exit_alert_points: int | None = None
    cross_basket_extra_points: int | None = None
    # Módulos
    orderbook_depth_points: int | None = None
    niche_market_points: int | None = None
    ob_min_depth_pct: float | None = None
    niche_max_liquidity: int | None = None
    niche_score_multiplier: float | None = None
    # Wallet Baskets
    basket_min_trades: int | None = None
    basket_shift_threshold: float | None = None
    basket_points: int | None = None
    basket_cross_min: int | None = None
    # Sniper DBSCAN
    sniper_time_window: int | None = None
    sniper_min_cluster: int | None = None
    sniper_min_size: int | None = None
    sniper_points: int | None = None
    # Smart Money
    smart_wallet_min_winrate: float | None = None
    cooldown_hours: int | None = None


# Mapeo: campo del body → (key en DB, atributo en config)
_CONFIG_MAP = {
    "min_size_usd": ("min_size_usd", "MIN_SIZE_USD"),
    "large_size_usd": ("large_size_usd", "LARGE_SIZE_USD"),
    "alert_threshold": ("alert_threshold", "ALERT_THRESHOLD"),
    "poll_interval": ("poll_interval", "POLL_INTERVAL"),
    "max_markets": ("max_markets", "MAX_MARKETS"),
    "fresh_wallet_points": ("fresh_wallet_points", "FRESH_WALLET_POINTS"),
    "large_size_points": ("large_size_points", "LARGE_SIZE_POINTS"),
    "market_anomaly_points": ("market_anomaly_points", "MARKET_ANOMALY_POINTS"),
    "wallet_shift_points": ("wallet_shift_points", "WALLET_SHIFT_POINTS"),
    "concentration_points": ("concentration_points", "CONCENTRATION_POINTS"),
    "time_proximity_points": ("time_proximity_points", "TIME_PROXIMITY_POINTS"),
    "cluster_points": ("cluster_points", "CLUSTER_POINTS"),
    "hit_rate_min_resolved": ("hit_rate_min_resolved", "HIT_RATE_MIN_RESOLVED"),
    "hit_rate_min_pct": ("hit_rate_min_pct", "HIT_RATE_MIN_PCT"),
    "hit_rate_points": ("hit_rate_points", "HIT_RATE_POINTS"),
    "contrarian_points": ("contrarian_points", "CONTRARIAN_POINTS"),
    "accumulation_points": ("accumulation_points", "ACCUMULATION_POINTS"),
    "proven_winner_min_resolved": ("proven_winner_min_resolved", "PROVEN_WINNER_MIN_RESOLVED"),
    "proven_winner_min_pct": ("proven_winner_min_pct", "PROVEN_WINNER_MIN_PCT"),
    "proven_winner_points": ("proven_winner_points", "PROVEN_WINNER_POINTS"),
    "multi_smart_points": ("multi_smart_points", "MULTI_SMART_POINTS"),
    "late_insider_points": ("late_insider_points", "LATE_INSIDER_POINTS"),
    "exit_alert_min_resolved": ("exit_alert_min_resolved", "EXIT_ALERT_MIN_RESOLVED"),
    "exit_alert_min_pct": ("exit_alert_min_pct", "EXIT_ALERT_MIN_PCT"),
    "exit_alert_points": ("exit_alert_points", "EXIT_ALERT_POINTS"),
    "cross_basket_extra_points": ("cross_basket_extra_points", "CROSS_BASKET_EXTRA_POINTS"),
    "orderbook_depth_points": ("orderbook_depth_points", "ORDERBOOK_DEPTH_POINTS"),
    "niche_market_points": ("niche_market_points", "NICHE_MARKET_POINTS"),
    "ob_min_depth_pct": ("ob_min_depth_pct", "ORDERBOOK_MIN_DEPTH_PCT"),
    "niche_max_liquidity": ("niche_max_liquidity", "NICHE_MAX_LIQUIDITY"),
    "niche_score_multiplier": ("niche_score_multiplier", "NICHE_SCORE_MULTIPLIER"),
    "basket_min_trades": ("basket_min_trades", "BASKET_MIN_WALLET_TRADES"),
    "basket_shift_threshold": ("basket_shift_threshold", "BASKET_CATEGORY_SHIFT_THRESHOLD"),
    "basket_points": ("basket_points", "BASKET_POINTS"),
    "basket_cross_min": ("basket_cross_min", "BASKET_CROSS_MIN"),
    "sniper_time_window": ("sniper_time_window", "SNIPER_TIME_WINDOW_SEC"),
    "sniper_min_cluster": ("sniper_min_cluster", "SNIPER_MIN_CLUSTER_SIZE"),
    "sniper_min_size": ("sniper_min_size", "SNIPER_MIN_TRADE_SIZE"),
    "sniper_points": ("sniper_points", "SNIPER_POINTS"),
    "smart_wallet_min_winrate": ("smart_wallet_min_winrate", "SMART_WALLET_MIN_WINRATE"),
    "cooldown_hours": ("cooldown_hours", "COOLDOWN_HOURS"),
}


@router.get("/api/config")
async def get_config(request: Request):
    db = request.app.state.db
    saved = await db.get_config()
    return {
        # Detección general
        "min_size_usd": config.MIN_SIZE_USD,
        "large_size_usd": config.LARGE_SIZE_USD,
        "alert_threshold": config.ALERT_THRESHOLD,
        "excluded_categories": saved.get("excluded_categories", "sports,nba,nfl,nhl,mlb,mls,soccer,esports,crypto-prices"),
        "poll_interval": config.POLL_INTERVAL,
        "max_markets": config.MAX_MARKETS,
        # Señales 1-7
        "fresh_wallet_points": config.FRESH_WALLET_POINTS,
        "large_size_points": config.LARGE_SIZE_POINTS,
        "market_anomaly_points": config.MARKET_ANOMALY_POINTS,
        "wallet_shift_points": config.WALLET_SHIFT_POINTS,
        "concentration_points": config.CONCENTRATION_POINTS,
        "time_proximity_points": config.TIME_PROXIMITY_POINTS,
        "cluster_points": config.CLUSTER_POINTS,
        # Señales 8-14
        "hit_rate_min_resolved": config.HIT_RATE_MIN_RESOLVED,
        "hit_rate_min_pct": config.HIT_RATE_MIN_PCT,
        "hit_rate_points": config.HIT_RATE_POINTS,
        "contrarian_points": config.CONTRARIAN_POINTS,
        "accumulation_points": config.ACCUMULATION_POINTS,
        "proven_winner_min_resolved": config.PROVEN_WINNER_MIN_RESOLVED,
        "proven_winner_min_pct": config.PROVEN_WINNER_MIN_PCT,
        "proven_winner_points": config.PROVEN_WINNER_POINTS,
        "multi_smart_points": config.MULTI_SMART_POINTS,
        "late_insider_points": config.LATE_INSIDER_POINTS,
        "exit_alert_min_resolved": config.EXIT_ALERT_MIN_RESOLVED,
        "exit_alert_min_pct": config.EXIT_ALERT_MIN_PCT,
        "exit_alert_points": config.EXIT_ALERT_POINTS,
        "cross_basket_extra_points": config.CROSS_BASKET_EXTRA_POINTS,
        # Módulos
        "orderbook_depth_points": config.ORDERBOOK_DEPTH_POINTS,
        "niche_market_points": config.NICHE_MARKET_POINTS,
        "ob_min_depth_pct": config.ORDERBOOK_MIN_DEPTH_PCT,
        "niche_max_liquidity": config.NICHE_MAX_LIQUIDITY,
        "niche_score_multiplier": config.NICHE_SCORE_MULTIPLIER,
        # Wallet Baskets
        "basket_min_trades": config.BASKET_MIN_WALLET_TRADES,
        "basket_shift_threshold": config.BASKET_CATEGORY_SHIFT_THRESHOLD,
        "basket_points": config.BASKET_POINTS,
        "basket_cross_min": config.BASKET_CROSS_MIN,
        # Sniper DBSCAN
        "sniper_time_window": config.SNIPER_TIME_WINDOW_SEC,
        "sniper_min_cluster": config.SNIPER_MIN_CLUSTER_SIZE,
        "sniper_min_size": config.SNIPER_MIN_TRADE_SIZE,
        "sniper_points": config.SNIPER_POINTS,
        # Smart Money
        "smart_wallet_min_winrate": config.SMART_WALLET_MIN_WINRATE,
        "cooldown_hours": config.COOLDOWN_HOURS,
    }


@router.post("/api/config")
async def update_config(request: Request, body: ConfigUpdate):
    db = request.app.state.db
    data = {}

    # Procesar todos los campos usando el mapeo
    body_dict = body.model_dump(exclude_none=True)
    for field_name, value in body_dict.items():
        if field_name == "excluded_categories":
            data["excluded_categories"] = value
            bot = request.app.state.bot
            if bot:
                bot._excluded_categories = {c.strip().lower() for c in value.split(",") if c.strip()}
            continue
        if field_name in _CONFIG_MAP:
            db_key, config_attr = _CONFIG_MAP[field_name]
            data[db_key] = str(value)
            setattr(config, config_attr, value)

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
async def update_features(request: Request, body: FeaturesUpdate):
    db = request.app.state.db
    updated = {}
    data = {}
    if body.orderbook_depth is not None:
        config.FEATURE_ORDERBOOK_DEPTH = body.orderbook_depth
        updated["orderbook_depth"] = body.orderbook_depth
        data["feature_orderbook_depth"] = str(body.orderbook_depth)
    if body.market_classification is not None:
        config.FEATURE_MARKET_CLASSIFICATION = body.market_classification
        updated["market_classification"] = body.market_classification
        data["feature_market_classification"] = str(body.market_classification)
    if body.wallet_baskets is not None:
        config.FEATURE_WALLET_BASKETS = body.wallet_baskets
        updated["wallet_baskets"] = body.wallet_baskets
        data["feature_wallet_baskets"] = str(body.wallet_baskets)
    if body.sniper_dbscan is not None:
        config.FEATURE_SNIPER_DBSCAN = body.sniper_dbscan
        updated["sniper_dbscan"] = body.sniper_dbscan
        data["feature_sniper_dbscan"] = str(body.sniper_dbscan)
    if body.crypto_arb is not None:
        config.FEATURE_CRYPTO_ARB = body.crypto_arb
        updated["crypto_arb"] = body.crypto_arb
        data["feature_crypto_arb"] = str(body.crypto_arb)
    if data:
        await db.set_config_bulk(data)
    return {"status": "ok", "updated": updated}


# ── Crypto Arb ───────────────────────────────────────────────

@router.get("/api/crypto-arb/stats")
async def crypto_arb_stats(request: Request):
    db = request.app.state.db
    try:
        db_stats = await db.get_crypto_arb_stats()
    except Exception:
        db_stats = {"total_signals": 0, "resolved": 0, "wins": 0, "win_rate": 0,
                    "total_pnl": 0, "signals_24h": 0, "pnl_24h": 0, "by_coin": []}
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
    try:
        return await db.get_crypto_signals_history(limit=limit, coin=coin)
    except Exception:
        return []


@router.delete("/api/crypto-arb/signals")
async def delete_crypto_signals(request: Request, older_than_hours: int = 24):
    """Borrar señales crypto más viejas que N horas."""
    db = request.app.state.db
    try:
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(hours=older_than_hours)
        async with db._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM crypto_signals WHERE created_at < $1", cutoff
            )
            deleted = int(result.split(" ")[-1]) if result else 0
        return {"status": "ok", "deleted": deleted, "older_than_hours": older_than_hours}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.delete("/api/reset-all")
async def reset_all_data(request: Request):
    """Borrar TODOS los datos: wallets, trades, baselines, markets, alertas."""
    db = request.app.state.db
    try:
        async with db._pool.acquire() as conn:
            counts = {}
            for table in ["trades", "wallets", "wallet_links", "market_baselines", "markets_tracked", "alerts"]:
                result = await conn.execute(f"DELETE FROM {table}")
                counts[table] = int(result.split(" ")[-1]) if result else 0
        return {"status": "ok", "deleted": counts}
    except Exception as e:
        return {"status": "error", "error": str(e)}


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
    if bot.backtester._running:
        return {"status": "running"}
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
        "strategy": config.CRYPTO_ARB_STRATEGY,
        "min_score": config.CRYPTO_ARB_MIN_SCORE,
        "entry_max_time_sec": config.CRYPTO_ARB_ENTRY_MAX_TIME,
        "min_distance_atr": config.CRYPTO_ARB_MIN_DISTANCE_ATR,
        "min_trend_consistency": config.CRYPTO_ARB_MIN_TREND_CONSISTENCY,
    }


class CryptoConfigUpdate(BaseModel):
    min_price_move_pct: float | None = None
    max_poly_odds: float | None = None
    min_confidence_pct: float | None = None
    paper_bet_size: float | None = None
    max_daily_signals: int | None = None
    telegram_alerts: bool | None = None
    strategy: str | None = None
    min_score: float | None = None
    entry_max_time_sec: int | None = None
    min_distance_atr: float | None = None
    min_trend_consistency: float | None = None


@router.post("/api/crypto-arb/config")
async def update_crypto_config(request: Request, body: CryptoConfigUpdate):
    db = request.app.state.db
    updated = {}
    data = {}
    if body.min_price_move_pct is not None:
        config.CRYPTO_ARB_MIN_MOVE_PCT = body.min_price_move_pct
        updated["min_price_move_pct"] = body.min_price_move_pct
        data["crypto_min_move_pct"] = str(body.min_price_move_pct)
    if body.max_poly_odds is not None:
        config.CRYPTO_ARB_MAX_POLY_ODDS = body.max_poly_odds
        updated["max_poly_odds"] = body.max_poly_odds
        data["crypto_max_poly_odds"] = str(body.max_poly_odds)
    if body.min_confidence_pct is not None:
        config.CRYPTO_ARB_MIN_CONFIDENCE = body.min_confidence_pct
        updated["min_confidence_pct"] = body.min_confidence_pct
        data["crypto_min_confidence"] = str(body.min_confidence_pct)
    if body.paper_bet_size is not None:
        config.CRYPTO_ARB_PAPER_BET = body.paper_bet_size
        updated["paper_bet_size"] = body.paper_bet_size
        data["crypto_paper_bet"] = str(body.paper_bet_size)
    if body.max_daily_signals is not None:
        config.CRYPTO_ARB_MAX_DAILY = body.max_daily_signals
        updated["max_daily_signals"] = body.max_daily_signals
        data["crypto_max_daily"] = str(body.max_daily_signals)
    if body.telegram_alerts is not None:
        config.CRYPTO_ARB_TELEGRAM = body.telegram_alerts
        updated["telegram_alerts"] = body.telegram_alerts
        data["crypto_telegram"] = str(body.telegram_alerts)
    if body.strategy is not None and body.strategy in ("divergence", "score"):
        config.CRYPTO_ARB_STRATEGY = body.strategy
        updated["strategy"] = body.strategy
        data["crypto_strategy"] = body.strategy
    if body.min_score is not None:
        config.CRYPTO_ARB_MIN_SCORE = body.min_score
        updated["min_score"] = body.min_score
        data["crypto_min_score"] = str(body.min_score)
    if body.entry_max_time_sec is not None:
        config.CRYPTO_ARB_ENTRY_MAX_TIME = body.entry_max_time_sec
        updated["entry_max_time_sec"] = body.entry_max_time_sec
        data["crypto_entry_max_time"] = str(body.entry_max_time_sec)
    if body.min_distance_atr is not None:
        config.CRYPTO_ARB_MIN_DISTANCE_ATR = body.min_distance_atr
        updated["min_distance_atr"] = body.min_distance_atr
        data["crypto_min_distance_atr"] = str(body.min_distance_atr)
    if body.min_trend_consistency is not None:
        config.CRYPTO_ARB_MIN_TREND_CONSISTENCY = body.min_trend_consistency
        updated["min_trend_consistency"] = body.min_trend_consistency
        data["crypto_min_trend_consistency"] = str(body.min_trend_consistency)
    if data:
        await db.set_config_bulk(data)
    return {"status": "ok", "updated": updated}


# ── Autotrading Config ─────────────────────────────────────────────

@router.get("/api/crypto-arb/autotrade-config")
async def get_autotrade_config(request: Request):
    """Obtener configuración de autotrading (sin exponer credenciales)."""
    db = request.app.state.db
    raw = await db.get_config_bulk([
        "at_enabled", "at_bet_size", "at_min_edge", "at_min_confidence",
        "at_max_odds", "at_max_positions", "at_order_type",
        "at_max_daily_loss", "at_max_daily_trades", "at_cooldown_sec",
        "at_coins", "at_api_key", "at_api_secret", "at_private_key", "at_passphrase",
    ])
    return {
        "enabled": raw.get("at_enabled") == "true",
        "bet_size": float(raw.get("at_bet_size", 5)),
        "min_edge": float(raw.get("at_min_edge", 15)),
        "min_confidence": float(raw.get("at_min_confidence", 75)),
        "max_odds": float(raw.get("at_max_odds", 0.55)),
        "max_positions": int(raw.get("at_max_positions", 3)),
        "order_type": raw.get("at_order_type", "limit"),
        "max_daily_loss": float(raw.get("at_max_daily_loss", 50)),
        "max_daily_trades": int(raw.get("at_max_daily_trades", 20)),
        "cooldown_sec": int(raw.get("at_cooldown_sec", 30)),
        "coins": raw.get("at_coins", "BTC,ETH,SOL").split(","),
        "api_key_set": bool(raw.get("at_api_key")),
        "api_secret_set": bool(raw.get("at_api_secret")),
        "private_key_set": bool(raw.get("at_private_key")),
        "passphrase_set": bool(raw.get("at_passphrase")),
        "balance": None,
        "open_positions": 0,
        "pnl_today": 0,
    }


@router.post("/api/crypto-arb/autotrade-config")
async def save_autotrade_config(request: Request):
    """Guardar configuración de autotrading."""
    db = request.app.state.db
    body = await request.json()
    data = {}
    if "enabled" in body:
        data["at_enabled"] = "true" if body["enabled"] else "false"
    if "bet_size" in body:
        data["at_bet_size"] = str(body["bet_size"])
    if "min_edge" in body:
        data["at_min_edge"] = str(body["min_edge"])
    if "min_confidence" in body:
        data["at_min_confidence"] = str(body["min_confidence"])
    if "max_odds" in body:
        data["at_max_odds"] = str(body["max_odds"])
    if "max_positions" in body:
        data["at_max_positions"] = str(body["max_positions"])
    if "order_type" in body:
        data["at_order_type"] = body["order_type"]
    if "max_daily_loss" in body:
        data["at_max_daily_loss"] = str(body["max_daily_loss"])
    if "max_daily_trades" in body:
        data["at_max_daily_trades"] = str(body["max_daily_trades"])
    if "cooldown_sec" in body:
        data["at_cooldown_sec"] = str(body["cooldown_sec"])
    if "coins" in body:
        data["at_coins"] = ",".join(body["coins"])
    if "api_key" in body:
        data["at_api_key"] = body["api_key"]
    if "api_secret" in body:
        data["at_api_secret"] = body["api_secret"]
    if "private_key" in body:
        data["at_private_key"] = body["private_key"]
    if "passphrase" in body:
        data["at_passphrase"] = body["passphrase"]
    if data:
        await db.set_config_bulk(data)
    return {"status": "ok"}


@router.get("/api/crypto-arb/autotrade-test")
async def test_autotrade_connection(request: Request):
    """Probar conexión a Polymarket CLOB con las credenciales guardadas."""
    db = request.app.state.db
    raw = await db.get_config_bulk(["at_api_key", "at_api_secret", "at_private_key"])
    if not raw.get("at_api_key") or not raw.get("at_api_secret"):
        return {"connected": False, "error": "No hay credenciales configuradas. Guarda tu API Key y Secret primero."}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://clob.polymarket.com/time")
            if resp.status_code == 200:
                return {
                    "connected": True,
                    "clob_time": resp.json(),
                    "balance": None,
                    "note": "Conexión al CLOB exitosa. Para balance se necesita autenticación con py-clob-client."
                }
            return {"connected": False, "error": f"CLOB respondió con status {resp.status_code}"}
    except Exception as e:
        return {"connected": False, "error": str(e)}
