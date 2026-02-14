"""FastAPI routes para el dashboard y API."""
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


# ── Health ───────────────────────────────────────────────────────────

@router.get("/api/health")
async def health():
    return {"status": "ok"}
