"""FastAPI routes para el dashboard y API."""
import asyncio
import json
import time as _time
from collections import defaultdict
from datetime import datetime, timezone
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from pathlib import Path

from src import config

# Rate limiting simple para auth endpoints
_auth_attempts: dict[str, list[float]] = defaultdict(list)
_AUTH_MAX_ATTEMPTS = 10  # max intentos
_AUTH_WINDOW_SEC = 300   # ventana de 5 minutos

router = APIRouter()

DASHBOARD_HTML = Path(__file__).parent.parent / "dashboard" / "index.html"


def _derive_wallet_address(pk: str) -> str:
    """Derivar dirección de wallet a partir de private key (sin crashear)."""
    if not pk or len(pk) < 64:
        return ""
    try:
        from eth_account import Account
        if not pk.startswith("0x"):
            pk = "0x" + pk
        return Account.from_key(pk).address
    except Exception:
        return ""


async def get_user_id(request: Request) -> int:
    """Extraer user_id del token de sesión. Retorna 1 si no hay auth (retrocompat)."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        return 1
    try:
        db = request.app.state.db
        user = await db.get_session_user(token)
        return user.get("id", 1) if user else 1
    except Exception:
        return 1


# ── Dashboard ────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard():
    if DASHBOARD_HTML.exists():
        return HTMLResponse(DASHBOARD_HTML.read_text())
    return HTMLResponse("<h1>Dashboard loading...</h1>")


# ── Auth ─────────────────────────────────────────────────────────────

def _check_rate_limit(ip: str) -> bool:
    """Retorna True si el IP excedió el límite de intentos."""
    now = _time.time()
    _auth_attempts[ip] = [t for t in _auth_attempts[ip] if now - t < _AUTH_WINDOW_SEC]
    if len(_auth_attempts[ip]) >= _AUTH_MAX_ATTEMPTS:
        return True
    _auth_attempts[ip].append(now)
    return False


@router.post("/api/auth/register")
async def register(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if _check_rate_limit(client_ip):
        return JSONResponse({"status": "error", "error": "Demasiados intentos. Espera 5 minutos."}, status_code=429)
    db = request.app.state.db
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    email = body.get("email", "")
    display_name = body.get("display_name", "")
    if not username or not password:
        return {"status": "error", "error": "Usuario y contraseña requeridos"}
    if len(password) < 4:
        return {"status": "error", "error": "Contraseña mínimo 4 caracteres"}
    result = await db.create_user(username, password, email, display_name)
    if "error" in result:
        return {"status": "error", "error": result["error"]}
    token = await db.create_session(result["id"])
    return {"status": "ok", "user": result, "token": token}


@router.post("/api/auth/login")
async def login(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if _check_rate_limit(client_ip):
        return JSONResponse({"status": "error", "error": "Demasiados intentos. Espera 5 minutos."}, status_code=429)
    db = request.app.state.db
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not username or not password:
        return {"status": "error", "error": "Usuario y contraseña requeridos"}
    result = await db.verify_user(username, password)
    if "error" in result:
        return {"status": "error", "error": result["error"]}
    token = await db.create_session(result["id"])
    return {"status": "ok", "user": result, "token": token}


@router.post("/api/auth/logout")
async def logout(request: Request):
    db = request.app.state.db
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token:
        await db.delete_session(token)
    return {"status": "ok"}


@router.get("/api/auth/me")
async def get_me(request: Request):
    db = request.app.state.db
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    user = await db.get_session_user(token)
    if not user:
        return {"status": "error", "error": "No autenticado"}
    return {"status": "ok", "user": user}


# ── Stats ────────────────────────────────────────────────────────────

@router.get("/api/stats")
async def get_stats(request: Request):
    db = request.app.state.db
    uid = await get_user_id(request)
    stats = await db.get_dashboard_stats(user_id=uid)
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
            "resolution_method": "gamma_slug → clob",
            "unresolved_signals": results,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── Alerts ───────────────────────────────────────────────────────────

@router.get("/api/alerts")
async def get_alerts(request: Request, limit: int = 50):
    db = request.app.state.db
    uid = await get_user_id(request)
    return await db.get_recent_alerts(limit=limit, user_id=uid)


@router.delete("/api/alerts")
async def delete_alerts(request: Request, older_than_hours: int = 24):
    """Borrar alertas más viejas que N horas (solo del usuario actual)."""
    db = request.app.state.db
    uid = await get_user_id(request)
    try:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        async with db._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM alerts WHERE created_at < $1 AND user_id = $2", cutoff, uid
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
    uid = await get_user_id(request)
    return await db.get_top_wallets(limit=limit, user_id=uid)


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
    uid = await get_user_id(request)
    return await db.get_alert_category_distribution(user_id=uid)


# ── Trades Feed ──────────────────────────────────────────────────────

@router.get("/api/trades/recent")
async def recent_trades(request: Request, limit: int = 100):
    db = request.app.state.db
    return await db.get_recent_trades_feed(limit=limit)


# ── Charts ───────────────────────────────────────────────────────────

@router.get("/api/charts/alerts-by-day")
async def alerts_by_day(request: Request, days: int = 30):
    db = request.app.state.db
    uid = await get_user_id(request)
    return await db.get_alerts_by_day(days=days, user_id=uid)


@router.get("/api/charts/score-distribution")
async def score_distribution(request: Request):
    db = request.app.state.db
    uid = await get_user_id(request)
    return await db.get_score_distribution(user_id=uid)


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
    uid = await get_user_id(request)
    saved = await db.get_config(user_id=uid)
    # Helper: leer de DB per-user con fallback a config global
    def _v(db_key, default):
        v = saved.get(db_key)
        if v is None:
            return default
        try:
            if isinstance(default, float):
                return float(v)
            if isinstance(default, int):
                return int(float(v))
            return v
        except (ValueError, TypeError):
            return default
    return {
        # Detección general
        "min_size_usd": _v("min_size_usd", config.MIN_SIZE_USD),
        "large_size_usd": _v("large_size_usd", config.LARGE_SIZE_USD),
        "alert_threshold": _v("alert_threshold", config.ALERT_THRESHOLD),
        "excluded_categories": saved.get("excluded_categories", "sports,nba,nfl,nhl,mlb,mls,soccer,esports,crypto-prices"),
        "poll_interval": _v("poll_interval", config.POLL_INTERVAL),
        "max_markets": _v("max_markets", config.MAX_MARKETS),
        # Señales 1-7
        "fresh_wallet_points": _v("fresh_wallet_points", config.FRESH_WALLET_POINTS),
        "large_size_points": _v("large_size_points", config.LARGE_SIZE_POINTS),
        "market_anomaly_points": _v("market_anomaly_points", config.MARKET_ANOMALY_POINTS),
        "wallet_shift_points": _v("wallet_shift_points", config.WALLET_SHIFT_POINTS),
        "concentration_points": _v("concentration_points", config.CONCENTRATION_POINTS),
        "time_proximity_points": _v("time_proximity_points", config.TIME_PROXIMITY_POINTS),
        "cluster_points": _v("cluster_points", config.CLUSTER_POINTS),
        # Señales 8-14
        "hit_rate_min_resolved": _v("hit_rate_min_resolved", config.HIT_RATE_MIN_RESOLVED),
        "hit_rate_min_pct": _v("hit_rate_min_pct", config.HIT_RATE_MIN_PCT),
        "hit_rate_points": _v("hit_rate_points", config.HIT_RATE_POINTS),
        "contrarian_points": _v("contrarian_points", config.CONTRARIAN_POINTS),
        "accumulation_points": _v("accumulation_points", config.ACCUMULATION_POINTS),
        "proven_winner_min_resolved": _v("proven_winner_min_resolved", config.PROVEN_WINNER_MIN_RESOLVED),
        "proven_winner_min_pct": _v("proven_winner_min_pct", config.PROVEN_WINNER_MIN_PCT),
        "proven_winner_points": _v("proven_winner_points", config.PROVEN_WINNER_POINTS),
        "multi_smart_points": _v("multi_smart_points", config.MULTI_SMART_POINTS),
        "late_insider_points": _v("late_insider_points", config.LATE_INSIDER_POINTS),
        "exit_alert_min_resolved": _v("exit_alert_min_resolved", config.EXIT_ALERT_MIN_RESOLVED),
        "exit_alert_min_pct": _v("exit_alert_min_pct", config.EXIT_ALERT_MIN_PCT),
        "exit_alert_points": _v("exit_alert_points", config.EXIT_ALERT_POINTS),
        "cross_basket_extra_points": _v("cross_basket_extra_points", config.CROSS_BASKET_EXTRA_POINTS),
        # Módulos
        "orderbook_depth_points": _v("orderbook_depth_points", config.ORDERBOOK_DEPTH_POINTS),
        "niche_market_points": _v("niche_market_points", config.NICHE_MARKET_POINTS),
        "ob_min_depth_pct": _v("ob_min_depth_pct", config.ORDERBOOK_MIN_DEPTH_PCT),
        "niche_max_liquidity": _v("niche_max_liquidity", config.NICHE_MAX_LIQUIDITY),
        "niche_score_multiplier": _v("niche_score_multiplier", config.NICHE_SCORE_MULTIPLIER),
        # Wallet Baskets
        "basket_min_trades": _v("basket_min_trades", config.BASKET_MIN_WALLET_TRADES),
        "basket_shift_threshold": _v("basket_shift_threshold", config.BASKET_CATEGORY_SHIFT_THRESHOLD),
        "basket_points": _v("basket_points", config.BASKET_POINTS),
        "basket_cross_min": _v("basket_cross_min", config.BASKET_CROSS_MIN),
        # Sniper DBSCAN
        "sniper_time_window": _v("sniper_time_window", config.SNIPER_TIME_WINDOW_SEC),
        "sniper_min_cluster": _v("sniper_min_cluster", config.SNIPER_MIN_CLUSTER_SIZE),
        "sniper_min_size": _v("sniper_min_size", config.SNIPER_MIN_TRADE_SIZE),
        "sniper_points": _v("sniper_points", config.SNIPER_POINTS),
        # Smart Money
        "smart_wallet_min_winrate": _v("smart_wallet_min_winrate", config.SMART_WALLET_MIN_WINRATE),
        "cooldown_hours": _v("cooldown_hours", config.COOLDOWN_HOURS),
    }


@router.post("/api/config")
async def update_config(request: Request, body: ConfigUpdate):
    db = request.app.state.db
    uid = await get_user_id(request)
    data = {}

    # Procesar todos los campos usando el mapeo
    body_dict = body.model_dump(exclude_none=True)
    for field_name, value in body_dict.items():
        if field_name == "excluded_categories":
            data["excluded_categories"] = value
            continue
        if field_name in _CONFIG_MAP:
            db_key, config_attr = _CONFIG_MAP[field_name]
            data[db_key] = str(value)
            # Solo aplicar a config global si es user 1 (retrocompat)
            if uid == 1:
                setattr(config, config_attr, value)

    if data:
        await db.set_config_bulk(data, user_id=uid)
    return {"status": "ok", "updated": list(data.keys())}


# ── Leaderboard ─────────────────────────────────────────────────────

@router.get("/api/leaderboard")
async def get_leaderboard(request: Request, limit: int = 30, sort: str = "pnl"):
    db = request.app.state.db
    uid = await get_user_id(request)
    return await db.get_leaderboard(limit=limit, sort_by=sort, user_id=uid)


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
    uid = await get_user_id(request)
    updated = {}
    data = {}
    if body.orderbook_depth is not None:
        if uid == 1: config.FEATURE_ORDERBOOK_DEPTH = body.orderbook_depth
        updated["orderbook_depth"] = body.orderbook_depth
        data["feature_orderbook_depth"] = str(body.orderbook_depth)
    if body.market_classification is not None:
        if uid == 1: config.FEATURE_MARKET_CLASSIFICATION = body.market_classification
        updated["market_classification"] = body.market_classification
        data["feature_market_classification"] = str(body.market_classification)
    if body.wallet_baskets is not None:
        if uid == 1: config.FEATURE_WALLET_BASKETS = body.wallet_baskets
        updated["wallet_baskets"] = body.wallet_baskets
        data["feature_wallet_baskets"] = str(body.wallet_baskets)
    if body.sniper_dbscan is not None:
        if uid == 1: config.FEATURE_SNIPER_DBSCAN = body.sniper_dbscan
        updated["sniper_dbscan"] = body.sniper_dbscan
        data["feature_sniper_dbscan"] = str(body.sniper_dbscan)
    if body.crypto_arb is not None:
        if uid == 1: config.FEATURE_CRYPTO_ARB = body.crypto_arb
        updated["crypto_arb"] = body.crypto_arb
        data["feature_crypto_arb"] = str(body.crypto_arb)
    if data:
        await db.set_config_bulk(data, user_id=uid)
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


@router.get("/api/crypto-arb/price-sum-arb")
async def crypto_price_sum_arb(request: Request):
    """Detectar oportunidades de Price-Sum Arbitrage (YES+NO != $1)."""
    detector = getattr(request.app.state, 'crypto_detector', None)
    if not detector:
        return {"status": "error", "error": "Crypto detector no activo"}
    try:
        opportunities = await detector.check_price_sum_arb()
        return {"status": "ok", "opportunities": opportunities}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.delete("/api/crypto-arb/signals")
async def delete_crypto_signals(request: Request, older_than_hours: int = 24):
    """Borrar señales crypto más viejas que N horas (solo admin)."""
    db = request.app.state.db
    uid = await get_user_id(request)
    if uid != 1:
        return {"status": "error", "error": "Solo el admin puede borrar señales"}
    try:
        from datetime import timedelta
        async with db._pool.acquire() as conn:
            if older_than_hours <= 0:
                result = await conn.execute("DELETE FROM crypto_signals")
                await conn.execute("DELETE FROM autotrades")
            else:
                cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
                result = await conn.execute(
                    "DELETE FROM crypto_signals WHERE created_at < $1", cutoff
                )
            deleted = int(result.split(" ")[-1]) if result else 0
        return {"status": "ok", "deleted": deleted, "older_than_hours": older_than_hours}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.delete("/api/crypto-arb/autotrades/reset")
async def reset_autotrades(request: Request):
    """Borrar TODOS los autotrades del usuario y resetear estado del autotrader."""
    db = request.app.state.db
    uid = await get_user_id(request)
    try:
        async with db._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM autotrades WHERE user_id = $1", uid)
            deleted = int(result.split(" ")[-1]) if result else 0
        # Resetear estado en memoria del autotrader
        bot = request.app.state.bot
        if bot and hasattr(bot, "autotrader") and bot.autotrader:
            bot.autotrader.reset_state()
        return {"status": "ok", "deleted": deleted, "message": f"Autotrades borrados: {deleted}. Estado reseteado."}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.delete("/api/reset-all")
async def reset_all_data(request: Request):
    """Borrar TODOS los datos del usuario actual."""
    db = request.app.state.db
    uid = await get_user_id(request)
    try:
        async with db._pool.acquire() as conn:
            counts = {}
            # Tablas con user_id — filtrar por usuario
            for table in ["alerts", "autotrades", "alert_autotrades"]:
                result = await conn.execute(f"DELETE FROM {table} WHERE user_id = $1", uid)
                counts[table] = int(result.split(" ")[-1]) if result else 0
            # Tablas globales (sin user_id) — solo borrar si es user 1
            if uid == 1:
                for table in ["trades", "wallets", "wallet_links", "market_baselines", "markets_tracked", "crypto_signals"]:
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
async def get_crypto_config(request: Request):
    db = request.app.state.db
    uid = await get_user_id(request)
    saved = await db.get_config(user_id=uid)
    def _v(db_key, default):
        v = saved.get(db_key)
        if v is None: return default
        try:
            if isinstance(default, float): return float(v)
            if isinstance(default, int): return int(float(v))
            if isinstance(default, bool): return v.lower() in ("true", "1", "yes")
            return v
        except (ValueError, TypeError): return default
    return {
        "mode": config.CRYPTO_ARB_MODE,
        "coins": config.CRYPTO_ARB_COINS,
        "min_price_move_pct": _v("crypto_min_move_pct", config.CRYPTO_ARB_MIN_MOVE_PCT),
        "max_poly_odds": _v("crypto_max_poly_odds", config.CRYPTO_ARB_MAX_POLY_ODDS),
        "min_confidence_pct": _v("crypto_min_confidence", config.CRYPTO_ARB_MIN_CONFIDENCE),
        "min_time_remaining_sec": config.CRYPTO_ARB_MIN_TIME_SEC,
        "max_time_remaining_sec": config.CRYPTO_ARB_MAX_TIME_SEC,
        "lookback_seconds": config.CRYPTO_ARB_LOOKBACK_SEC,
        "paper_bet_size": _v("crypto_paper_bet", config.CRYPTO_ARB_PAPER_BET),
        "max_daily_signals": _v("crypto_max_daily", config.CRYPTO_ARB_MAX_DAILY),
        "telegram_alerts": _v("crypto_telegram", config.CRYPTO_ARB_TELEGRAM),
        "strategy": _v("crypto_strategy", config.CRYPTO_ARB_STRATEGY),
        "min_score": _v("crypto_min_score", config.CRYPTO_ARB_MIN_SCORE),
        "entry_max_time_sec": _v("crypto_entry_max_time", config.CRYPTO_ARB_ENTRY_MAX_TIME),
        "min_distance_atr": _v("crypto_min_distance_atr", config.CRYPTO_ARB_MIN_DISTANCE_ATR),
        "min_trend_consistency": _v("crypto_min_trend_consistency", config.CRYPTO_ARB_MIN_TREND_CONSISTENCY),
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
    uid = await get_user_id(request)
    updated = {}
    data = {}
    if body.min_price_move_pct is not None:
        if uid == 1: config.CRYPTO_ARB_MIN_MOVE_PCT = body.min_price_move_pct
        updated["min_price_move_pct"] = body.min_price_move_pct
        data["crypto_min_move_pct"] = str(body.min_price_move_pct)
    if body.max_poly_odds is not None:
        if uid == 1: config.CRYPTO_ARB_MAX_POLY_ODDS = body.max_poly_odds
        updated["max_poly_odds"] = body.max_poly_odds
        data["crypto_max_poly_odds"] = str(body.max_poly_odds)
    if body.min_confidence_pct is not None:
        if uid == 1: config.CRYPTO_ARB_MIN_CONFIDENCE = body.min_confidence_pct
        updated["min_confidence_pct"] = body.min_confidence_pct
        data["crypto_min_confidence"] = str(body.min_confidence_pct)
    if body.paper_bet_size is not None:
        if uid == 1: config.CRYPTO_ARB_PAPER_BET = body.paper_bet_size
        updated["paper_bet_size"] = body.paper_bet_size
        data["crypto_paper_bet"] = str(body.paper_bet_size)
    if body.max_daily_signals is not None:
        if uid == 1: config.CRYPTO_ARB_MAX_DAILY = body.max_daily_signals
        updated["max_daily_signals"] = body.max_daily_signals
        data["crypto_max_daily"] = str(body.max_daily_signals)
    if body.telegram_alerts is not None:
        if uid == 1: config.CRYPTO_ARB_TELEGRAM = body.telegram_alerts
        updated["telegram_alerts"] = body.telegram_alerts
        data["crypto_telegram"] = str(body.telegram_alerts)
    if body.strategy is not None and body.strategy in ("divergence", "score"):
        if uid == 1: config.CRYPTO_ARB_STRATEGY = body.strategy
        updated["strategy"] = body.strategy
        data["crypto_strategy"] = body.strategy
    if body.min_score is not None:
        if uid == 1: config.CRYPTO_ARB_MIN_SCORE = body.min_score
        updated["min_score"] = body.min_score
        data["crypto_min_score"] = str(body.min_score)
    if body.entry_max_time_sec is not None:
        if uid == 1: config.CRYPTO_ARB_ENTRY_MAX_TIME = body.entry_max_time_sec
        updated["entry_max_time_sec"] = body.entry_max_time_sec
        data["crypto_entry_max_time"] = str(body.entry_max_time_sec)
    if body.min_distance_atr is not None:
        if uid == 1: config.CRYPTO_ARB_MIN_DISTANCE_ATR = body.min_distance_atr
        updated["min_distance_atr"] = body.min_distance_atr
        data["crypto_min_distance_atr"] = str(body.min_distance_atr)
    if body.min_trend_consistency is not None:
        if uid == 1: config.CRYPTO_ARB_MIN_TREND_CONSISTENCY = body.min_trend_consistency
        updated["min_trend_consistency"] = body.min_trend_consistency
        data["crypto_min_trend_consistency"] = str(body.min_trend_consistency)
    if data:
        await db.set_config_bulk(data, user_id=uid)
    return {"status": "ok", "updated": updated}


# ── Autotrading Config ─────────────────────────────────────────────

@router.get("/api/crypto-arb/autotrade-config")
async def get_autotrade_config(request: Request):
    """Obtener configuración de autotrading (sin exponer credenciales)."""
    db = request.app.state.db
    uid = await get_user_id(request)
    raw = await db.get_config_bulk([
        "at_enabled", "at_bet_size", "at_min_edge", "at_min_confidence",
        "at_max_odds", "at_max_positions", "at_order_type",
        "at_max_daily_loss", "at_max_daily_trades", "at_cooldown_sec",
        "at_coins", "at_api_key", "at_api_secret", "at_private_key", "at_passphrase",
        "at_funder_address",
        "at_stop_loss_enabled", "at_stop_loss_pct", "at_take_profit_pct",
        "at_max_holding_sec", "at_trailing_stop_enabled", "at_trailing_stop_pct",
        "at_slippage_max_pct",
    ], user_id=uid)
    # Estadísticas reales del autotrader
    stats = await db.get_autotrade_stats(user_id=uid)
    # Estado del motor
    autotrader = getattr(request.app.state, 'autotrader', None)
    at_status = autotrader.get_status() if autotrader else {}
    # Consultar saldo USDC real
    wallet_addr = _derive_wallet_address(raw.get("at_private_key", ""))
    balance = None
    if autotrader and wallet_addr:
        try:
            balance = await autotrader.get_usdc_balance(wallet_addr)
        except Exception:
            pass
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
        "api_key_preview": (raw.get("at_api_key", "")[:12] + "...") if raw.get("at_api_key") else "",
        "wallet_address": wallet_addr,
        "connected": at_status.get("connected", False),
        "balance": balance,
        "open_positions": stats.get("open_positions", 0),
        "pnl_today": stats.get("pnl_24h", 0),
        "trades_today": stats.get("trades_24h", 0),
        "total_pnl": stats.get("total_pnl", 0),
        "win_rate": stats.get("win_rate", 0),
        "stop_loss_enabled": raw.get("at_stop_loss_enabled") == "true",
        "stop_loss_pct": float(raw.get("at_stop_loss_pct", 25)),
        "take_profit_pct": float(raw.get("at_take_profit_pct", 30)),
        "max_holding_sec": int(raw.get("at_max_holding_sec", 1800)),
        "trailing_stop_enabled": raw.get("at_trailing_stop_enabled") == "true",
        "trailing_stop_pct": float(raw.get("at_trailing_stop_pct", 15)),
        "slippage_max_pct": float(raw.get("at_slippage_max_pct", 3.0)),
        "funder_address": raw.get("at_funder_address", ""),
        "funder_address_set": bool(raw.get("at_funder_address")),
    }


@router.post("/api/crypto-arb/autotrade-config")
async def save_autotrade_config(request: Request):
    """Guardar configuración de autotrading per-user."""
    db = request.app.state.db
    uid = await get_user_id(request)
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
    if "funder_address" in body:
        data["at_funder_address"] = body["funder_address"]
    # Stop-Loss / Take-Profit / Risk Management
    if "stop_loss_enabled" in body:
        data["at_stop_loss_enabled"] = "true" if body["stop_loss_enabled"] else "false"
    if "stop_loss_pct" in body:
        data["at_stop_loss_pct"] = str(body["stop_loss_pct"])
    if "take_profit_pct" in body:
        data["at_take_profit_pct"] = str(body["take_profit_pct"])
    if "max_holding_sec" in body:
        data["at_max_holding_sec"] = str(body["max_holding_sec"])
    if "trailing_stop_enabled" in body:
        data["at_trailing_stop_enabled"] = "true" if body["trailing_stop_enabled"] else "false"
    if "trailing_stop_pct" in body:
        data["at_trailing_stop_pct"] = str(body["trailing_stop_pct"])
    if "slippage_max_pct" in body:
        data["at_slippage_max_pct"] = str(body["slippage_max_pct"])
    if data:
        await db.set_config_bulk(data, user_id=uid)
    # Recargar config en el autotrader
    autotrader = getattr(request.app.state, 'autotrader', None)
    if autotrader:
        try:
            await autotrader.reload_config(user_id=uid)
        except Exception:
            pass
    return {"status": "ok"}


@router.post("/api/crypto-arb/generate-keys")
async def generate_api_keys(request: Request):
    """Generar API Key, Secret y Passphrase a partir de la Private Key.
    Solo necesita la private key de la wallet — genera las otras 3 credenciales
    automáticamente via EIP-712 signing + CLOB API.
    """
    import httpx as _httpx
    db = request.app.state.db
    uid = await get_user_id(request)
    body = await request.json()
    private_key = body.get("private_key", "").strip()

    if not private_key:
        return {"status": "error", "error": "Private key es requerida."}

    # Limpiar formato
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    pk_clean = private_key[2:]
    if len(pk_clean) != 64:
        return {"status": "error", "error": f"Private key debe tener 64 caracteres hex (sin 0x). La tuya tiene {len(pk_clean)}."}

    try:
        import time as _time
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        account = Account.from_key(private_key)
        wallet_address = account.address
        timestamp = str(int(_time.time()))
        nonce = 0

        # EIP-712 structured data para CLOB auth (L1)
        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                ],
                "ClobAuth": [
                    {"name": "address", "type": "address"},
                    {"name": "timestamp", "type": "string"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "message", "type": "string"},
                ],
            },
            "primaryType": "ClobAuth",
            "domain": {"name": "ClobAuthDomain", "version": "1", "chainId": 137},
            "message": {
                "address": wallet_address,
                "timestamp": timestamp,
                "nonce": nonce,
                "message": "This message attests that I control the given wallet",
            },
        }

        signable = encode_typed_data(full_message=typed_data)
        signed = account.sign_message(signable)
        signature = signed.signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature

        # Headers para CLOB L1 auth
        poly_headers = {
            "POLY_ADDRESS": wallet_address,
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": timestamp,
            "POLY_NONCE": str(nonce),
            "User-Agent": "PolymarketAlertBot/3.0",
            "Accept": "application/json",
        }

        api_creds = None
        error_msg = ""

        async with _httpx.AsyncClient(timeout=15) as client:
            # Intentar derivar primero (si ya existen keys)
            try:
                resp = await client.get("https://clob.polymarket.com/auth/derive-api-key", headers=poly_headers)
                if resp.status_code == 200:
                    api_creds = resp.json()
            except Exception:
                pass

            # Si no se pudieron derivar, crear nuevas
            if not api_creds:
                try:
                    resp = await client.post("https://clob.polymarket.com/auth/api-key", headers=poly_headers, content=b"")
                    if resp.status_code == 200:
                        api_creds = resp.json()
                    else:
                        error_msg = f"CLOB respondió HTTP {resp.status_code}: {resp.text[:200]}"
                except Exception as e:
                    error_msg = str(e)

        if not api_creds:
            return {"status": "error", "error": error_msg or "No se pudieron generar las API keys."}

        # Log full response para debug
        print(f"[GenerateKeys] CLOB response keys: {list(api_creds.keys())}", flush=True)

        # Guardar las 4 credenciales en DB
        api_key = api_creds.get("apiKey", "")
        api_secret = api_creds.get("secret", "")
        passphrase = api_creds.get("passphrase", "")

        # Intentar detectar proxy wallet address (funder) automáticamente
        funder_address = ""
        try:
            # Método 1: usar py-clob-client create_or_derive_api_creds que puede dar más info
            from py_clob_client.client import ClobClient as _ClobClient
            from py_clob_client.clob_types import ApiCreds as _ApiCreds
            _creds = _ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=passphrase)
            _temp_client = _ClobClient(
                "https://clob.polymarket.com",
                key=private_key,
                chain_id=137,
                signature_type=2,
                creds=_creds,
            )
            # Intentar get_api_keys() que puede devolver info del proxy
            try:
                api_keys_resp = _temp_client.get_api_keys()
                print(f"[GenerateKeys] get_api_keys response: {api_keys_resp}", flush=True)
                # Buscar proxyAddress o funder en la respuesta
                if isinstance(api_keys_resp, list):
                    for k in api_keys_resp:
                        if isinstance(k, dict):
                            for field in ["proxyAddress", "funder", "proxy_address", "makerAddress", "maker"]:
                                if k.get(field):
                                    funder_address = k[field]
                                    print(f"[GenerateKeys] Proxy wallet detectada: {funder_address}", flush=True)
                                    break
                        if funder_address:
                            break
                elif isinstance(api_keys_resp, dict):
                    for field in ["proxyAddress", "funder", "proxy_address", "makerAddress", "maker"]:
                        if api_keys_resp.get(field):
                            funder_address = api_keys_resp[field]
                            print(f"[GenerateKeys] Proxy wallet detectada: {funder_address}", flush=True)
                            break
            except Exception as e2:
                print(f"[GenerateKeys] get_api_keys falló: {e2}", flush=True)
        except Exception as e1:
            print(f"[GenerateKeys] Detección de proxy falló: {e1}", flush=True)

        # Método 2: si no se detectó, intentar via Gamma API
        if not funder_address:
            try:
                async with _httpx.AsyncClient(timeout=10) as gamma_client:
                    gamma_resp = await gamma_client.get(
                        f"https://gamma-api.polymarket.com/nonce",
                        params={"address": wallet_address}
                    )
                    if gamma_resp.status_code == 200:
                        gamma_data = gamma_resp.json()
                        print(f"[GenerateKeys] Gamma nonce response: {gamma_data}", flush=True)
                        for field in ["proxyAddress", "proxy_address", "polyAddress", "address"]:
                            val = gamma_data.get(field, "")
                            if val and val.lower() != wallet_address.lower():
                                funder_address = val
                                print(f"[GenerateKeys] Proxy via Gamma: {funder_address}", flush=True)
                                break
            except Exception as e3:
                print(f"[GenerateKeys] Gamma API falló: {e3}", flush=True)

        config_to_save = {
            "at_private_key": pk_clean,
            "at_api_key": api_key,
            "at_api_secret": api_secret,
            "at_passphrase": passphrase,
        }
        if funder_address:
            config_to_save["at_funder_address"] = funder_address

        await db.set_config_bulk(config_to_save, user_id=uid)

        # Recargar config en autotrader
        autotrader = getattr(request.app.state, 'autotrader', None)
        if autotrader:
            try:
                await autotrader.reload_config(user_id=uid)
            except Exception:
                pass

        msg = "API Keys generadas y guardadas."
        if funder_address:
            msg += f" Proxy wallet detectada: {funder_address[:10]}..."
        else:
            msg += " ⚠️ Proxy wallet NO detectada. Ve a polymarket.com/settings, copia tu Proxy Wallet Address y guárdala en Config."

        return {
            "status": "ok",
            "wallet": wallet_address,
            "api_key_preview": api_key[:12] + "..." if api_key else "",
            "funder_address": funder_address,
            "funder_detected": bool(funder_address),
            "message": msg,
        }

    except ImportError:
        return {"status": "error", "error": "Dependencia eth-account no instalada. Ejecuta: pip install eth-account"}
    except ValueError as e:
        return {"status": "error", "error": f"Private key inválida: {e}"}
    except Exception as e:
        return {"status": "error", "error": f"Error generando keys: {e}"}


@router.get("/api/crypto-arb/autotrade-test")
async def test_autotrade_connection(request: Request):
    """Probar conexión a Polymarket CLOB + diagnóstico completo de balance."""
    import httpx
    db = request.app.state.db
    uid = await get_user_id(request)
    raw = await db.get_config_bulk(["at_api_key", "at_api_secret", "at_private_key"], user_id=uid)

    if not raw.get("at_api_key") or not raw.get("at_api_secret"):
        return {"connected": False, "error": "No hay credenciales configuradas."}

    wallet_addr = _derive_wallet_address(raw.get("at_private_key", ""))
    debug = {"wallet": wallet_addr, "checks": []}

    # 1. Test CLOB
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://clob.polymarket.com/time")
            debug["clob_status"] = resp.status_code
            if resp.status_code != 200:
                return {"connected": False, "error": f"CLOB status {resp.status_code}", "debug": debug}
    except Exception as e:
        return {"connected": False, "error": f"CLOB error: {e}", "debug": debug}

    # 2. Balance USDC — diagnóstico detallado por contrato y RPC
    from src.crypto_arb.autotrader import USDC_CONTRACTS
    contract_names = {
        "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174": "USDC.e (bridged)",
        "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359": "USDC (native)",
    }
    rpc_url = "https://polygon-rpc.com"
    total_balance = 0.0
    addr_padded = wallet_addr.lower().replace("0x", "").zfill(64) if wallet_addr else ""

    if wallet_addr:
        # También consultar MATIC balance para verificar que la address es correcta
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                matic_payload = {
                    "jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
                    "params": [wallet_addr, "latest"]
                }
                r = await client.post(rpc_url, json=matic_payload)
                if r.status_code == 200:
                    matic_raw = r.json().get("result", "0x0")
                    matic_bal = int(matic_raw, 16) / 1e18
                    debug["matic_balance"] = round(matic_bal, 6)
                else:
                    debug["matic_error"] = f"HTTP {r.status_code}"
        except Exception as e:
            debug["matic_error"] = str(e)

        # Consultar cada contrato USDC
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for contract in USDC_CONTRACTS:
                    check = {"contract": contract, "name": contract_names.get(contract, "?")}
                    try:
                        payload = {
                            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                            "params": [{
                                "to": contract,
                                "data": f"0x70a08231000000000000000000000000{addr_padded}"
                            }, "latest"]
                        }
                        r = await client.post(rpc_url, json=payload)
                        check["rpc_status"] = r.status_code
                        if r.status_code == 200:
                            body = r.json()
                            result = body.get("result", "0x0")
                            check["raw_result"] = result
                            if body.get("error"):
                                check["rpc_error"] = body["error"]
                            if result and result != "0x":
                                bal = int(result, 16) / 1e6
                                check["balance_usdc"] = round(bal, 6)
                                total_balance += bal
                            else:
                                check["balance_usdc"] = 0
                        else:
                            check["response"] = r.text[:200]
                    except Exception as e:
                        check["error"] = str(e)
                    debug["checks"].append(check)
        except Exception as e:
            debug["rpc_global_error"] = str(e)

    # 3. CLOB balance (Polymarket internal) — usar autotrader o crear cliente temporal
    import asyncio
    clob_client = None
    autotrader = getattr(request.app.state, 'autotrader', None)
    if autotrader and autotrader._client:
        clob_client = autotrader._client
        debug["autotrader_active"] = True
    else:
        debug["autotrader_active"] = False
        # Crear cliente CLOB temporal con las credenciales guardadas
        try:
            full_raw = await db.get_config_bulk([
                "at_api_key", "at_api_secret", "at_passphrase", "at_private_key"
            ], user_id=uid)
            pk = full_raw.get("at_private_key", "")
            ak = full_raw.get("at_api_key", "")
            ase = full_raw.get("at_api_secret", "")
            pp = full_raw.get("at_passphrase", "")
            if pk and ak and ase:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import ApiCreds
                creds = ApiCreds(api_key=ak, api_secret=ase, api_passphrase=pp)
                clob_client = ClobClient(
                    "https://clob.polymarket.com",
                    key=pk if pk.startswith("0x") else "0x" + pk,
                    chain_id=137,
                    signature_type=2,
                    creds=creds,
                )
                debug["temp_client"] = True
        except Exception as e:
            debug["temp_client_error"] = str(e)

    if clob_client:
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            loop = asyncio.get_running_loop()
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            ba = await loop.run_in_executor(None, clob_client.get_balance_allowance, params)
            debug["clob_balance_raw"] = str(ba)
            poly_bal = 0.0
            if ba and hasattr(ba, "balance"):
                raw_bal = float(ba.balance)
                poly_bal = raw_bal / 1e6 if raw_bal > 1_000 else raw_bal
            elif isinstance(ba, dict) and "balance" in ba:
                raw_bal = float(ba["balance"])
                poly_bal = raw_bal / 1e6 if raw_bal > 1_000 else raw_bal
            if poly_bal > 0:
                total_balance += poly_bal
            debug["polymarket_balance"] = round(poly_bal, 2)
        except Exception as e:
            debug["clob_balance_error"] = str(e)

    balance = round(total_balance, 2)
    return {
        "connected": True,
        "balance": balance,
        "wallet": wallet_addr,
        "note": "Conexión exitosa." if balance > 0 else "Conectado pero balance $0 — ver debug",
        "debug": debug,
    }


@router.get("/api/crypto-arb/autotrades")
async def get_autotrades(request: Request, hours: int = 48, limit: int = 50):
    """Obtener historial de autotrades ejecutados."""
    db = request.app.state.db
    uid = await get_user_id(request)
    trades = await db.get_autotrades(hours=hours, limit=limit, user_id=uid)
    stats = await db.get_autotrade_stats(user_id=uid)
    return {"trades": trades, "stats": stats}


# ── Alert AutoTrader endpoints ──────────────────────────────────────

@router.get("/api/alert-trading/config")
async def get_alert_trading_config(request: Request):
    """Obtener configuración del alert autotrader."""
    db = request.app.state.db
    uid = await get_user_id(request)
    raw = await db.get_config_bulk([
        "aat_enabled", "aat_bet_size", "aat_min_score",
        "aat_max_odds", "aat_min_odds", "aat_max_positions",
        "aat_max_daily_trades", "aat_max_daily_loss",
        "aat_min_wallet_hit_rate", "aat_cooldown_hours",
        "aat_excluded_categories", "aat_require_smart_money",
        "aat_take_profit_enabled", "aat_take_profit_pct", "aat_stop_loss_pct",
        "aat_confirm_enabled", "aat_confirm_hours", "aat_confirm_min_pct", "aat_confirm_max_hours",
        "aat_kelly_enabled", "aat_kelly_base_fraction",
        "aat_trailing_stop_enabled", "aat_trailing_stop_pct",
        "aat_partial_tp_enabled", "aat_partial_tp_pct", "aat_partial_tp_fraction",
        "aat_auto_exit_on_sell", "aat_min_market_liquidity",
        "aat_auto_scale_enabled", "aat_auto_scale_win_boost", "aat_auto_scale_loss_reduce",
        "aat_max_category_exposure", "aat_max_drawdown",
        "aat_api_key", "aat_private_key",
    ], user_id=uid)
    stats = await db.get_alert_autotrade_stats(user_id=uid)
    aat = getattr(request.app.state, 'alert_autotrader', None)
    aat_status = aat.get_status() if aat else {}
    has_own_wallet = bool(raw.get("aat_private_key"))
    return {
        "enabled": raw.get("aat_enabled") == "true",
        "bet_size": float(raw.get("aat_bet_size", 10)),
        "min_score": int(raw.get("aat_min_score", 7)),
        "max_odds": float(raw.get("aat_max_odds", 0.80)),
        "min_odds": float(raw.get("aat_min_odds", 0.15)),
        "max_positions": int(raw.get("aat_max_positions", 5)),
        "max_daily_trades": int(raw.get("aat_max_daily_trades", 5)),
        "max_daily_loss": float(raw.get("aat_max_daily_loss", 50)),
        "min_wallet_hit_rate": float(raw.get("aat_min_wallet_hit_rate", 0)),
        "cooldown_hours": float(raw.get("aat_cooldown_hours", 6)),
        "excluded_categories": raw.get("aat_excluded_categories", ""),
        "require_smart_money": raw.get("aat_require_smart_money") == "true",
        "take_profit_enabled": raw.get("aat_take_profit_enabled") == "true",
        "take_profit_pct": float(raw.get("aat_take_profit_pct", 0)),
        "stop_loss_pct": float(raw.get("aat_stop_loss_pct", 0)),
        "confirm_enabled": raw.get("aat_confirm_enabled") == "true",
        "confirm_hours": float(raw.get("aat_confirm_hours", 1)),
        "confirm_min_pct": float(raw.get("aat_confirm_min_pct", 3)),
        "confirm_max_hours": float(raw.get("aat_confirm_max_hours", 6)),
        "pending_confirmations": aat_status.get("pending_confirmations", 0),
        # Kelly Criterion
        "kelly_enabled": raw.get("aat_kelly_enabled") == "true",
        "kelly_base_fraction": float(raw.get("aat_kelly_base_fraction", 0.25)),
        # Trailing Stop Loss
        "trailing_stop_enabled": raw.get("aat_trailing_stop_enabled") == "true",
        "trailing_stop_pct": float(raw.get("aat_trailing_stop_pct", 15)),
        # Partial Profit Taking
        "partial_tp_enabled": raw.get("aat_partial_tp_enabled") == "true",
        "partial_tp_pct": float(raw.get("aat_partial_tp_pct", 30)),
        "partial_tp_fraction": float(raw.get("aat_partial_tp_fraction", 50)),
        # Auto Exit on insider SELL
        "auto_exit_on_sell": raw.get("aat_auto_exit_on_sell") == "true",
        # Filtro liquidez
        "min_market_liquidity": float(raw.get("aat_min_market_liquidity", 0)),
        # Auto-scaling
        "auto_scale_enabled": raw.get("aat_auto_scale_enabled") == "true",
        "auto_scale_win_boost": float(raw.get("aat_auto_scale_win_boost", 10)),
        "auto_scale_loss_reduce": float(raw.get("aat_auto_scale_loss_reduce", 20)),
        # Diversificación
        "max_category_exposure": int(raw.get("aat_max_category_exposure", 0)),
        # Max drawdown
        "max_drawdown": float(raw.get("aat_max_drawdown", 0)),
        "drawdown_paused": aat_status.get("drawdown_paused", False),
        "has_own_wallet": has_own_wallet,
        "wallet_connected": has_own_wallet,
        "wallet_address": _derive_wallet_address(raw.get("aat_private_key", "")),
        "api_key_preview": (raw.get("aat_api_key", "")[:12] + "...") if raw.get("aat_api_key") else "",
        "connected": aat_status.get("connected", False),
        "open_positions": stats.get("open_positions", 0),
        "pnl_today": stats.get("pnl_24h", 0),
        "trades_today": stats.get("trades_24h", 0),
        "total_pnl": stats.get("total_pnl", 0),
        "total_trades": stats.get("total_trades", 0),
        "wins": stats.get("wins", 0),
        "losses": stats.get("losses", 0),
        "win_rate": stats.get("win_rate", 0),
        "total_volume": stats.get("total_volume", 0),
    }


@router.post("/api/alert-trading/config")
async def save_alert_trading_config(request: Request):
    """Guardar configuración del alert autotrader per-user."""
    db = request.app.state.db
    uid = await get_user_id(request)
    body = await request.json()
    data = {}
    if "enabled" in body:
        data["aat_enabled"] = "true" if body["enabled"] else "false"
    if "bet_size" in body:
        data["aat_bet_size"] = str(body["bet_size"])
    if "min_score" in body:
        data["aat_min_score"] = str(body["min_score"])
    if "max_odds" in body:
        data["aat_max_odds"] = str(body["max_odds"])
    if "min_odds" in body:
        data["aat_min_odds"] = str(body["min_odds"])
    if "max_positions" in body:
        data["aat_max_positions"] = str(body["max_positions"])
    if "max_daily_trades" in body:
        data["aat_max_daily_trades"] = str(body["max_daily_trades"])
    if "max_daily_loss" in body:
        data["aat_max_daily_loss"] = str(body["max_daily_loss"])
    if "min_wallet_hit_rate" in body:
        data["aat_min_wallet_hit_rate"] = str(body["min_wallet_hit_rate"])
    if "cooldown_hours" in body:
        data["aat_cooldown_hours"] = str(body["cooldown_hours"])
    if "excluded_categories" in body:
        data["aat_excluded_categories"] = body["excluded_categories"]
    if "require_smart_money" in body:
        data["aat_require_smart_money"] = "true" if body["require_smart_money"] else "false"
    if "take_profit_enabled" in body:
        data["aat_take_profit_enabled"] = "true" if body["take_profit_enabled"] else "false"
    if "take_profit_pct" in body:
        data["aat_take_profit_pct"] = str(body["take_profit_pct"])
    if "stop_loss_pct" in body:
        data["aat_stop_loss_pct"] = str(body["stop_loss_pct"])
    if "confirm_enabled" in body:
        data["aat_confirm_enabled"] = "true" if body["confirm_enabled"] else "false"
    if "confirm_hours" in body:
        data["aat_confirm_hours"] = str(body["confirm_hours"])
    if "confirm_min_pct" in body:
        data["aat_confirm_min_pct"] = str(body["confirm_min_pct"])
    if "confirm_max_hours" in body:
        data["aat_confirm_max_hours"] = str(body["confirm_max_hours"])
    # Kelly Criterion
    if "kelly_enabled" in body:
        data["aat_kelly_enabled"] = "true" if body["kelly_enabled"] else "false"
    if "kelly_base_fraction" in body:
        data["aat_kelly_base_fraction"] = str(body["kelly_base_fraction"])
    # Trailing Stop Loss
    if "trailing_stop_enabled" in body:
        data["aat_trailing_stop_enabled"] = "true" if body["trailing_stop_enabled"] else "false"
    if "trailing_stop_pct" in body:
        data["aat_trailing_stop_pct"] = str(body["trailing_stop_pct"])
    # Partial Profit Taking
    if "partial_tp_enabled" in body:
        data["aat_partial_tp_enabled"] = "true" if body["partial_tp_enabled"] else "false"
    if "partial_tp_pct" in body:
        data["aat_partial_tp_pct"] = str(body["partial_tp_pct"])
    if "partial_tp_fraction" in body:
        data["aat_partial_tp_fraction"] = str(body["partial_tp_fraction"])
    # Auto Exit
    if "auto_exit_on_sell" in body:
        data["aat_auto_exit_on_sell"] = "true" if body["auto_exit_on_sell"] else "false"
    # Filtro liquidez
    if "min_market_liquidity" in body:
        data["aat_min_market_liquidity"] = str(body["min_market_liquidity"])
    # Auto-scaling
    if "auto_scale_enabled" in body:
        data["aat_auto_scale_enabled"] = "true" if body["auto_scale_enabled"] else "false"
    if "auto_scale_win_boost" in body:
        data["aat_auto_scale_win_boost"] = str(body["auto_scale_win_boost"])
    if "auto_scale_loss_reduce" in body:
        data["aat_auto_scale_loss_reduce"] = str(body["auto_scale_loss_reduce"])
    # Diversificación
    if "max_category_exposure" in body:
        data["aat_max_category_exposure"] = str(body["max_category_exposure"])
    # Max drawdown
    if "max_drawdown" in body:
        data["aat_max_drawdown"] = str(body["max_drawdown"])
    if data:
        await db.set_config_bulk(data, user_id=uid)
    # Recargar config en alert autotrader (solo user 1 retrocompat)
    if uid == 1:
        aat = getattr(request.app.state, 'alert_autotrader', None)
        if aat:
            try:
                await aat.reload_config()
            except Exception:
                pass
    return {"status": "ok"}


@router.get("/api/alert-trading/backtest")
async def alert_backtest(request: Request, days: int = 30, min_score: int = 5, bet_size: float = 10):
    """Backtesting simulado: qué habría pasado si operábamos con alertas históricas."""
    db = request.app.state.db
    try:
        alerts = await db.get_backtest_data(days, min_score)
        if not alerts:
            return {"status": "ok", "alerts": [], "summary": {}}

        total_invested = 0
        total_pnl = 0
        wins = 0
        losses = 0
        open_count = 0
        by_score = {}

        for a in alerts:
            sim_res = a["sim_result"]
            entry = a["entry_price"]
            if entry <= 0:
                continue

            shares = bet_size / entry
            if sim_res == "win":
                pnl = (1.0 - entry) * shares
                wins += 1
                total_invested += bet_size
                total_pnl += pnl
                a["sim_pnl_usd"] = round(pnl, 2)
            elif sim_res == "loss":
                pnl = -bet_size
                losses += 1
                total_invested += bet_size
                total_pnl += pnl
                a["sim_pnl_usd"] = round(pnl, 2)
            else:
                exit_p = a.get("exit_price")
                if exit_p is not None:
                    pnl = (exit_p - entry) * shares
                    a["sim_pnl_usd"] = round(pnl, 2)
                    total_invested += bet_size
                    total_pnl += pnl
                else:
                    a["sim_pnl_usd"] = 0
                open_count += 1

            # Agrupar por score
            s = a["score"]
            if s not in by_score:
                by_score[s] = {"wins": 0, "losses": 0, "pnl": 0, "count": 0}
            by_score[s]["count"] += 1
            by_score[s]["pnl"] += a.get("sim_pnl_usd", 0)
            if sim_res == "win":
                by_score[s]["wins"] += 1
            elif sim_res == "loss":
                by_score[s]["losses"] += 1

        resolved = wins + losses
        win_rate = round((wins / resolved) * 100, 1) if resolved > 0 else 0
        roi = round((total_pnl / total_invested) * 100, 1) if total_invested > 0 else 0

        summary = {
            "total_alerts": len(alerts),
            "resolved": resolved,
            "open": open_count,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_invested": round(total_invested, 2),
            "total_pnl": round(total_pnl, 2),
            "roi": roi,
            "bet_size": bet_size,
            "days": days,
            "min_score": min_score,
            "by_score": [{"score": k, **v, "pnl": round(v["pnl"], 2)} for k, v in sorted(by_score.items())],
        }
        return {"status": "ok", "alerts": alerts[:200], "summary": summary}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.get("/api/alert-trading/pnl-history")
async def get_alert_pnl_history(request: Request, days: int = 30):
    """Historial de PnL diario para gráfico de evolución."""
    db = request.app.state.db
    uid = await get_user_id(request)
    try:
        history = await db.get_alert_pnl_history(days, user_id=uid)
        return {"status": "ok", "history": history}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.get("/api/alert-trading/wallet-ranking")
async def get_alert_wallet_ranking(request: Request, limit: int = 20):
    """Ranking de wallets por profit generado en copy-trades."""
    db = request.app.state.db
    try:
        ranking = await db.get_alert_wallet_ranking(limit)
        for r in ranking:
            for k in r:
                if isinstance(r[k], (float,)) and r[k] != r[k]:  # NaN check
                    r[k] = 0
                elif hasattr(r[k], '__float__'):
                    r[k] = round(float(r[k]), 2)
        return {"status": "ok", "ranking": ranking}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.post("/api/alert-trading/generate-keys")
async def generate_aat_api_keys(request: Request):
    """Generar API Keys propias para Alert Trading (wallet separada)."""
    import httpx as _httpx
    db = request.app.state.db
    uid = await get_user_id(request)
    body = await request.json()
    private_key = body.get("private_key", "").strip()
    if not private_key:
        return {"status": "error", "error": "Private key es requerida."}
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    pk_clean = private_key[2:]
    if len(pk_clean) != 64:
        return {"status": "error", "error": f"Private key debe tener 64 caracteres hex. La tuya tiene {len(pk_clean)}."}
    try:
        import time as _time
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        account = Account.from_key(private_key)
        wallet_address = account.address
        timestamp = str(int(_time.time()))
        nonce = 0
        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                ],
                "ClobAuth": [
                    {"name": "address", "type": "address"},
                    {"name": "timestamp", "type": "string"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "message", "type": "string"},
                ],
            },
            "primaryType": "ClobAuth",
            "domain": {"name": "ClobAuthDomain", "version": "1", "chainId": 137},
            "message": {
                "address": wallet_address,
                "timestamp": timestamp,
                "nonce": nonce,
                "message": "This message attests that I control the given wallet",
            },
        }
        signable = encode_typed_data(full_message=typed_data)
        signed = account.sign_message(signable)
        signature = signed.signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature
        poly_headers = {
            "POLY_ADDRESS": wallet_address,
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": timestamp,
            "POLY_NONCE": str(nonce),
            "User-Agent": "PolymarketAlertBot/3.0",
            "Accept": "application/json",
        }
        api_creds = None
        error_msg = ""
        async with _httpx.AsyncClient(timeout=15) as client:
            try:
                resp = await client.get("https://clob.polymarket.com/auth/derive-api-key", headers=poly_headers)
                if resp.status_code == 200:
                    api_creds = resp.json()
            except Exception:
                pass
            if not api_creds:
                try:
                    resp = await client.post("https://clob.polymarket.com/auth/api-key", headers=poly_headers, content=b"")
                    if resp.status_code == 200:
                        api_creds = resp.json()
                    else:
                        error_msg = f"CLOB HTTP {resp.status_code}: {resp.text[:200]}"
                except Exception as e:
                    error_msg = str(e)
        if not api_creds:
            return {"status": "error", "error": error_msg or "No se pudieron generar las API keys."}
        await db.set_config_bulk({
            "aat_private_key": pk_clean,
            "aat_api_key": api_creds.get("apiKey", ""),
            "aat_api_secret": api_creds.get("secret", ""),
            "aat_passphrase": api_creds.get("passphrase", ""),
        }, user_id=uid)
        aat = getattr(request.app.state, 'alert_autotrader', None)
        if aat:
            try:
                await aat.reload_config()
            except Exception:
                pass
        return {"status": "ok", "wallet": wallet_address, "message": "Wallet propia configurada para Alert Trading."}
    except Exception as e:
        return {"status": "error", "error": f"Error: {e}"}


@router.post("/api/alert-trading/disconnect-wallet")
async def disconnect_aat_wallet(request: Request):
    """Desconectar wallet propia del Alert Trading (vuelve a usar la compartida)."""
    db = request.app.state.db
    uid = await get_user_id(request)
    await db.set_config_bulk({
        "aat_private_key": "",
        "aat_api_key": "",
        "aat_api_secret": "",
        "aat_passphrase": "",
    }, user_id=uid)
    aat = getattr(request.app.state, 'alert_autotrader', None)
    if aat:
        try:
            await aat.reload_config()
        except Exception:
            pass
    return {"status": "ok", "message": "Wallet propia desconectada. Usando wallet de Crypto Arb si existe."}


@router.get("/api/alert-trading/trades")
async def get_alert_trades(request: Request, hours: int = 168, limit: int = 50):
    """Obtener historial de alert autotrades (default última semana)."""
    db = request.app.state.db
    uid = await get_user_id(request)
    trades = await db.get_alert_autotrades(hours=hours, limit=limit, user_id=uid)
    stats = await db.get_alert_autotrade_stats(user_id=uid)
    return {"trades": trades, "stats": stats}


# ══════════════════════════════════════════════════════════════════════
# ── v8.0: Nuevas Features ────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════

@router.get("/api/heatmap")
async def get_heatmap(request: Request):
    db = request.app.state.db
    uid = await get_user_id(request)
    data = await db.get_heatmap_data(user_id=uid)
    return data

@router.get("/api/journal")
async def get_journal(request: Request, limit: int = 50):
    db = request.app.state.db
    uid = await get_user_id(request)
    notes = await db.get_journal_notes(user_id=uid, limit=limit)
    return notes

@router.post("/api/journal")
async def add_journal(request: Request):
    db = request.app.state.db
    uid = await get_user_id(request)
    body = await request.json()
    await db.add_journal_note(
        trade_type=body.get("trade_type", "manual"),
        trade_id=body.get("trade_id", 0),
        note=body.get("note", ""),
        tags=body.get("tags", ""),
        user_id=uid,
    )
    return {"status": "ok"}

@router.get("/api/bankroll")
async def get_bankroll(request: Request, days: int = 30):
    db = request.app.state.db
    uid = await get_user_id(request)
    history = await db.get_bankroll_history(days=days, user_id=uid)
    bankroll = getattr(request.app.state, 'bankroll', None)
    stats = bankroll.get_stats() if bankroll else {"enabled": False}
    return {"stats": stats, "history": history}

@router.get("/api/notifications")
async def get_notifications(request: Request, unread: bool = True):
    db = request.app.state.db
    uid = await get_user_id(request)
    notifs = await db.get_notifications(user_id=uid, unread_only=unread)
    return notifs

@router.post("/api/notifications/read")
async def mark_read(request: Request):
    db = request.app.state.db
    uid = await get_user_id(request)
    body = await request.json()
    ids = body.get("ids")
    await db.mark_notifications_read(user_id=uid, notification_ids=ids)
    return {"status": "ok"}

@router.get("/api/strategies/stats")
async def get_strategies_stats(request: Request):
    """Stats de todos los bots/estrategias adicionales."""
    result = {}
    for name in ['market_maker', 'spike_detector', 'event_driven', 'cross_platform']:
        bot = getattr(request.app.state, name, None)
        if bot:
            result[name] = bot.get_stats()
        else:
            result[name] = {"enabled": False}
    return result

@router.get("/api/infra/stats")
async def get_infra_stats(request: Request):
    """Stats de módulos de infraestructura."""
    result = {}
    for name in ['rate_limiter', 'queue', 'websocket', 'bankroll', 'news_catalyst', 'ml_scorer']:
        mod = getattr(request.app.state, name, None)
        if mod:
            result[name] = mod.get_stats()
        else:
            result[name] = {"enabled": False}
    return result

@router.get("/api/features/v8")
async def get_features_v8(request: Request):
    """Obtener estado de features v8.0 configurables (12 toggleables + 6 always-on)."""
    db = request.app.state.db
    uid = await get_user_id(request)
    saved = await db.get_config(user_id=uid)
    _b = lambda k, default: saved[k].lower() in ("true", "1", "yes") if k in saved else default
    return {
        # Always-on (info only)
        "always_on": ["correlation_filter", "rate_limiting", "bankroll_tracking", "heatmap", "trade_journal", "push_notifications"],
        # Toggleables (per-user desde DB)
        "multi_timeframe": _b("feature_multi_timeframe", config.FEATURE_MULTI_TIMEFRAME),
        "vwap": _b("feature_vwap", config.FEATURE_VWAP),
        "orderbook_crypto": _b("feature_orderbook_crypto", config.FEATURE_ORDERBOOK_CRYPTO),
        "hedging": _b("feature_hedging", config.FEATURE_HEDGING),
        "news_catalyst": _b("feature_news_catalyst", config.FEATURE_NEWS_CATALYST),
        "ml_scoring": _b("feature_ml_scoring", config.FEATURE_ML_SCORING),
        "websocket": _b("feature_websocket", config.FEATURE_WEBSOCKET),
        "queue_system": _b("feature_queue", config.FEATURE_QUEUE),
        "market_making": _b("feature_market_making", config.FEATURE_MARKET_MAKING),
        "event_driven": _b("feature_event_driven", config.FEATURE_EVENT_DRIVEN),
        "spike_detection": _b("feature_spike_detection", config.FEATURE_SPIKE_DETECTION),
        "cross_platform_arb": _b("feature_cross_platform", config.FEATURE_CROSS_PLATFORM),
        "rsi": _b("feature_rsi", config.FEATURE_RSI),
        "macd": _b("feature_macd", config.FEATURE_MACD),
    }

@router.post("/api/features/v8")
async def save_features_v8(request: Request):
    """Guardar estado de features v8.0 per-user."""
    db = request.app.state.db
    uid = await get_user_id(request)
    body = await request.json()
    mapping = {
        "multi_timeframe": "feature_multi_timeframe",
        "vwap": "feature_vwap",
        "orderbook_crypto": "feature_orderbook_crypto",
        "hedging": "feature_hedging",
        "news_catalyst": "feature_news_catalyst",
        "ml_scoring": "feature_ml_scoring",
        "websocket": "feature_websocket",
        "queue_system": "feature_queue",
        "market_making": "feature_market_making",
        "event_driven": "feature_event_driven",
        "spike_detection": "feature_spike_detection",
        "cross_platform_arb": "feature_cross_platform",
        "rsi": "feature_rsi",
        "macd": "feature_macd",
    }
    import src.config as cfg
    save_data = {}
    for ui_key, cfg_key in mapping.items():
        if ui_key in body:
            val = body[ui_key]
            save_data[cfg_key] = str(val).lower()
            # Solo aplicar a config global si es user 1 (retrocompat)
            if uid == 1:
                setattr(cfg, cfg_key.upper(), val)
    await db.set_config(save_data, user_id=uid)
    return {"status": "ok"}

@router.get("/api/mm/orders")
async def get_mm_orders(request: Request, limit: int = 50):
    db = request.app.state.db
    uid = await get_user_id(request)
    orders = await db.get_mm_orders(limit=limit, user_id=uid)
    mm = getattr(request.app.state, 'market_maker', None)
    stats = mm.get_stats() if mm else {"running": False}
    return {"orders": orders, "stats": stats}


# ══════════════════════════════════════════════════════════════════════
# ── Telegram Config ───────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════

@router.get("/api/telegram/config")
async def get_telegram_config(request: Request):
    db = request.app.state.db
    uid = await get_user_id(request)
    saved = await db.get_config(user_id=uid)
    token = saved.get("telegram_bot_token", "")
    chat_ids = saved.get("telegram_chat_ids", "")
    # Enmascarar token para seguridad (mostrar solo últimos 8 chars)
    masked = ""
    if token and len(token) > 8:
        masked = "*" * (len(token) - 8) + token[-8:]
    return {"bot_token": masked, "chat_ids": chat_ids, "configured": bool(token and chat_ids)}

@router.post("/api/telegram/config")
async def save_telegram_config(request: Request):
    db = request.app.state.db
    uid = await get_user_id(request)
    body = await request.json()
    token = body.get("bot_token", "").strip()
    chat_ids = body.get("chat_ids", "").strip()
    # Si el token viene enmascarado (****...), no sobreescribir
    save_data = {}
    if token and not token.startswith("*"):
        save_data["telegram_bot_token"] = token
        if uid == 1:
            config.TELEGRAM_BOT_TOKEN = token
    if chat_ids:
        save_data["telegram_chat_ids"] = chat_ids
        if uid == 1:
            config.TELEGRAM_CHAT_IDS = chat_ids
    if save_data:
        await db.set_config(save_data, user_id=uid)
    # Recargar el notifier solo si es user 1 (retrocompat)
    if uid == 1:
        bot = getattr(request.app.state, 'bot', None)
        if bot and bot.notifier:
            bot.notifier._token = config.TELEGRAM_BOT_TOKEN
            bot.notifier._chat_ids = [c.strip() for c in config.TELEGRAM_CHAT_IDS.split(",") if c.strip()]
    return {"status": "ok"}

@router.post("/api/telegram/test")
async def test_telegram(request: Request):
    import httpx
    db = request.app.state.db
    uid = await get_user_id(request)
    saved = await db.get_config(user_id=uid)
    # Leer token per-user desde DB con fallback a config global
    token = saved.get("telegram_bot_token") or config.TELEGRAM_BOT_TOKEN
    chat_ids_str = saved.get("telegram_chat_ids") or config.TELEGRAM_CHAT_IDS
    if not token:
        return {"status": "error", "error": "No hay Bot Token configurado"}
    if not chat_ids_str:
        return {"status": "error", "error": "No hay Chat ID configurado"}
    chat_ids = [c.strip() for c in chat_ids_str.split(",") if c.strip()]
    try:
        async with httpx.AsyncClient() as client:
            for cid in chat_ids:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": cid, "text": "Polymarket Alert Bot - Test de conexion exitoso!", "parse_mode": "HTML"},
                    timeout=10,
                )
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── Kalshi Arbitrage ─────────────────────────────────────────────

@router.get("/api/kalshi/arb")
async def get_kalshi_arb(request: Request):
    """Escanear oportunidades de arbitraje cross-platform Polymarket vs Kalshi.
    Compara precios de eventos similares en ambas plataformas.
    """
    import httpx
    from datetime import datetime, timezone

    min_edge = config.CROSS_PLATFORM_MIN_EDGE
    opportunities = []
    markets_scanned = 0

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # 1. Obtener eventos activos de Kalshi
            kalshi_events = []
            try:
                resp = await client.get(
                    "https://trading-api.kalshi.com/trade-api/v2/events",
                    params={"status": "open", "limit": 50},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    kalshi_events = data.get("events", [])
            except Exception:
                # Kalshi API puede requerir auth o cambiar — fallback a mercados
                try:
                    resp = await client.get(
                        "https://trading-api.kalshi.com/trade-api/v2/markets",
                        params={"status": "open", "limit": 100},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        kalshi_markets = data.get("markets", [])
                        # Agrupar por categoría para matching
                        for km in kalshi_markets:
                            kalshi_events.append({
                                "title": km.get("title", ""),
                                "ticker": km.get("ticker", ""),
                                "yes_ask": km.get("yes_ask", 0) / 100.0 if km.get("yes_ask") else None,
                                "no_ask": km.get("no_ask", 0) / 100.0 if km.get("no_ask") else None,
                                "category": km.get("category", ""),
                            })
                except Exception:
                    pass

            if not kalshi_events:
                return {
                    "opportunities": [],
                    "markets_scanned": 0,
                    "last_scan": datetime.now(timezone.utc).isoformat(),
                    "error": "No se pudo conectar a Kalshi API (puede requerir auth)"
                }

            # 2. Obtener mercados populares de Polymarket para comparar
            poly_markets = []
            try:
                resp2 = await client.get(
                    f"{config.GAMMA_API_URL}/markets",
                    params={"active": True, "closed": False, "limit": 100, "order": "liquidity", "ascending": False},
                )
                if resp2.status_code == 200:
                    poly_markets = resp2.json()
            except Exception:
                pass

            markets_scanned = len(kalshi_events) + len(poly_markets)

            # 3. Matching por palabras clave en títulos
            for ke in kalshi_events:
                k_title = (ke.get("title") or ke.get("event_title", "")).lower()
                k_yes = ke.get("yes_ask") or ke.get("yes_price", 0)
                if isinstance(k_yes, (int, float)) and k_yes > 1:
                    k_yes = k_yes / 100.0  # Kalshi usa centavos
                if not k_title or not k_yes:
                    continue

                # Buscar match en Polymarket
                for pm in poly_markets:
                    p_question = (pm.get("question") or "").lower()
                    p_price = None

                    # Extraer precio YES de Polymarket
                    op = pm.get("outcomePrices")
                    if op:
                        try:
                            if isinstance(op, str):
                                prices = json.loads(op)
                            else:
                                prices = op
                            if prices:
                                p_price = float(prices[0])
                        except Exception:
                            pass

                    if not p_price:
                        continue

                    # Matching simple: al menos 3 palabras significativas en comun
                    k_words = set(w for w in k_title.split() if len(w) > 3)
                    p_words = set(w for w in p_question.split() if len(w) > 3)
                    common = k_words & p_words
                    if len(common) < 3:
                        continue

                    # Calcular edge
                    edge_pct = abs(p_price - k_yes) * 100
                    if edge_pct < min_edge:
                        continue

                    # Determinar accion
                    if p_price < k_yes:
                        action = "Comprar YES en Poly"
                    else:
                        action = "Comprar YES en Kalshi"

                    opportunities.append({
                        "event": pm.get("question", "")[:80],
                        "poly_price": round(p_price, 3),
                        "kalshi_price": round(k_yes, 3),
                        "edge_pct": round(edge_pct, 1),
                        "action": action,
                        "category": ke.get("category", pm.get("groupItemTitle", "")),
                        "kalshi_ticker": ke.get("ticker", ""),
                        "poly_id": pm.get("conditionId", ""),
                    })

            # Ordenar por edge descendente
            opportunities.sort(key=lambda x: -x["edge_pct"])

    except Exception as e:
        return {
            "opportunities": [],
            "markets_scanned": markets_scanned,
            "last_scan": datetime.now(timezone.utc).isoformat(),
            "error": str(e),
        }

    return {
        "opportunities": opportunities[:30],
        "markets_scanned": markets_scanned,
        "last_scan": datetime.now(timezone.utc).isoformat(),
    }
