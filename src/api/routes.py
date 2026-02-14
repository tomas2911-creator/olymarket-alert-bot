"""FastAPI routes para el dashboard y API."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pathlib import Path

router = APIRouter()

DASHBOARD_HTML = Path(__file__).parent.parent / "dashboard" / "index.html"


@router.get("/", response_class=HTMLResponse)
async def dashboard():
    """Servir dashboard HTML."""
    if DASHBOARD_HTML.exists():
        return HTMLResponse(DASHBOARD_HTML.read_text())
    return HTMLResponse("<h1>Dashboard loading...</h1>")


@router.get("/api/stats")
async def get_stats(request: Request):
    """Estadísticas generales del dashboard."""
    db = request.app.state.db
    stats = await db.get_dashboard_stats()
    # Agregar uptime del bot
    bot = request.app.state.bot
    if bot:
        from datetime import datetime
        uptime = datetime.now() - bot.start_time
        hours = int(uptime.total_seconds() // 3600)
        minutes = int((uptime.total_seconds() % 3600) // 60)
        stats["uptime"] = f"{hours}h {minutes}m"
        stats["trades_this_session"] = bot.trades_processed
        stats["alerts_this_session"] = bot.alerts_sent
    return stats


@router.get("/api/alerts")
async def get_alerts(request: Request, limit: int = 50):
    """Alertas recientes."""
    db = request.app.state.db
    return await db.get_recent_alerts(limit=limit)


@router.get("/api/alerts/market/{market_id}")
async def get_market_alerts(request: Request, market_id: str):
    """Alertas de un mercado específico."""
    db = request.app.state.db
    return await db.get_market_alerts(market_id)


@router.get("/api/wallets")
async def get_top_wallets(request: Request, limit: int = 20):
    """Top wallets flaggeadas."""
    db = request.app.state.db
    return await db.get_top_wallets(limit=limit)


@router.get("/api/charts/alerts-by-day")
async def alerts_by_day(request: Request, days: int = 30):
    """Alertas por día para chart."""
    db = request.app.state.db
    return await db.get_alerts_by_day(days=days)


@router.get("/api/charts/score-distribution")
async def score_distribution(request: Request):
    """Distribución de scores para chart."""
    db = request.app.state.db
    return await db.get_score_distribution()


@router.get("/api/health")
async def health():
    return {"status": "ok"}
