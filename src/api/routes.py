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
import httpx

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
        if bot.ws_client:
            ws_stats = bot.ws_client.get_stats()
            stats["ws_trades"] = ws_stats.get("trades_received", 0)
            stats["ws_connected"] = ws_stats.get("connected", False)
        # Pipeline stats del último ciclo
        if bot._pipeline_stats:
            stats["pipeline"] = bot._pipeline_stats
        # Debug counters
        stats["debug"] = {
            "too_small": bot._debug.get("too_small", 0),
            "scored": bot._debug.get("scored", 0),
            "low_score": bot._debug.get("low_score", 0),
            "excluded_cat": bot._debug.get("excluded_cat", 0),
            "cooldown": bot._debug.get("cooldown", 0),
        }
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
    """Borrar alertas más viejas que N horas (0 = borrar todas)."""
    db = request.app.state.db
    uid = await get_user_id(request)
    try:
        async with db._pool.acquire() as conn:
            if older_than_hours <= 0:
                result = await conn.execute(
                    "DELETE FROM alerts WHERE user_id = $1", uid
                )
            else:
                from datetime import timedelta
                cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
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


# ── Wallet Tracker ────────────────────────────────────────────────────

@router.get("/api/wallet-tracker")
async def get_wallet_tracker(request: Request, min_trades: int = 1,
                              min_winrate: float = 0, sort_by: str = "pnl"):
    db = request.app.state.db
    uid = await get_user_id(request)
    wallets = await db.get_wallet_tracker(user_id=uid, min_trades=min_trades,
                                           min_winrate=min_winrate, sort_by=sort_by)
    return {"wallets": wallets, "total": len(wallets)}


@router.post("/api/wallet-tracker/watchlist/{address}")
async def toggle_watchlist(request: Request, address: str):
    db = request.app.state.db
    new_status = await db.toggle_wallet_watchlist(address)
    # Actualizar watchlist en memoria del bot inmediatamente (no esperar 30 min)
    bot = getattr(request.app.state, "bot", None)
    if bot and hasattr(bot, "_watchlist"):
        addr_lower = address.lower()
        if new_status:
            bot._watchlist.add(addr_lower)
        else:
            bot._watchlist.discard(addr_lower)
    return {"address": address, "is_watchlisted": new_status}


@router.get("/api/wallet-tracker/{address}/trades")
async def get_wallet_tracker_trades(request: Request, address: str):
    db = request.app.state.db
    uid = await get_user_id(request)
    trades = await db.get_wallet_trades_detail(address, user_id=uid)
    return {"trades": trades}


# ── Whale Trades ─────────────────────────────────────────────────────

@router.get("/api/whales/feed")
async def get_whale_feed(request: Request, min_size: float = 10000,
                          hours: int = 24, side: str = "", limit: int = 200):
    db = request.app.state.db
    trades = await db.get_whale_feed(min_size=min_size, hours=hours, side=side, limit=limit)
    return {"trades": trades, "total": len(trades)}


@router.get("/api/whales/ranking")
async def get_whale_ranking(request: Request, min_size: float = 10000,
                             min_trades: int = 1, min_winrate: float = 0,
                             sort_by: str = "volume"):
    db = request.app.state.db
    wallets = await db.get_whale_ranking(min_size=min_size, min_trades=min_trades,
                                          min_winrate=min_winrate, sort_by=sort_by)
    return {"wallets": wallets, "total": len(wallets)}


@router.get("/api/whales/stats")
async def get_whale_stats(request: Request, hours: int = 24, min_size: float = 10000):
    db = request.app.state.db
    stats = await db.get_whale_stats(hours=hours, min_size=min_size)
    return stats


@router.get("/api/whales/wallet/{address}/history")
async def get_whale_wallet_history(request: Request, address: str, limit: int = 50):
    db = request.app.state.db
    data = await db.get_whale_wallet_history(address=address, limit=limit)
    return data


# ── Wallet Scan ──────────────────────────────────────────────────────

_POLY_DATA_API = "https://data-api.polymarket.com"
_POLY_GAMMA_API = "https://gamma-api.polymarket.com"
_POLY_CLOB_API = "https://clob.polymarket.com"


@router.get("/api/wallet/scan")
async def wallet_scan(request: Request, address: str):
    """Análisis completo de una wallet de Polymarket consultando la Data API."""
    addr = address.strip().lower()
    if not addr or len(addr) < 10:
        return JSONResponse({"error": "Dirección inválida"}, status_code=400)

    async with httpx.AsyncClient(timeout=25) as client:
        # ── Fetch paralelo de datos independientes ──
        async def _fetch_portfolio():
            try:
                r = await client.get(f"{_POLY_DATA_API}/value", params={"user": addr})
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and data:
                        return float(data[0].get("value", 0))
                    elif isinstance(data, dict):
                        return float(data.get("value", 0))
            except Exception:
                pass
            return 0

        async def _fetch_leaderboard():
            """PnL oficial y volumen desde el leaderboard de Polymarket."""
            try:
                r = await client.get(f"{_POLY_DATA_API}/v1/leaderboard",
                                     params={"user": addr, "period": "ALL", "order": "PNL"})
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and data:
                        return data[0]
            except Exception:
                pass
            return None

        async def _fetch_traded():
            """Número real de mercados operados desde /traded."""
            try:
                r = await client.get(f"{_POLY_DATA_API}/traded", params={"user": addr})
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict):
                        return int(data.get("traded", 0))
            except Exception:
                pass
            return 0

        async def _fetch_profile():
            """Perfil público desde Gamma API."""
            try:
                r = await client.get(f"{_POLY_GAMMA_API}/public-profile",
                                     params={"address": addr})
                if r.status_code == 200:
                    return r.json()
            except Exception:
                pass
            return None

        async def _fetch_positions():
            try:
                r = await client.get(f"{_POLY_DATA_API}/positions",
                                     params={"user": addr, "sizeThreshold": 0.01, "limit": 500,
                                             "sortBy": "CURRENT", "sortDirection": "DESC"})
                if r.status_code == 200:
                    d = r.json()
                    return d if isinstance(d, list) else []
            except Exception:
                pass
            return []

        async def _fetch_closed():
            closed = []
            try:
                offset = 0
                max_closed = 2500
                while offset < max_closed:
                    r = await client.get(f"{_POLY_DATA_API}/closed-positions",
                                         params={"user": addr, "limit": 50, "offset": offset,
                                                 "sortBy": "TIMESTAMP", "sortDirection": "DESC"})
                    if r.status_code != 200:
                        break
                    d = r.json()
                    page = d if isinstance(d, list) else []
                    if not page:
                        break
                    closed.extend(page)
                    if len(page) < 50:
                        break
                    offset += 50
            except Exception:
                pass
            return closed

        async def _fetch_activity():
            try:
                r = await client.get(f"{_POLY_DATA_API}/activity",
                                     params={"user": addr, "limit": 100, "offset": 0})
                if r.status_code == 200:
                    d = r.json()
                    return d if isinstance(d, list) else []
            except Exception:
                pass
            return []

        # Ejecutar todo en paralelo
        (portfolio_value, leaderboard, traded_count, profile,
         positions, closed_positions, activity_recent) = await asyncio.gather(
            _fetch_portfolio(), _fetch_leaderboard(), _fetch_traded(),
            _fetch_profile(), _fetch_positions(), _fetch_closed(), _fetch_activity()
        )

        # ── Perfil del trader ──
        profile_name = ""
        profile_image = ""
        profile_username = ""
        if profile and isinstance(profile, dict):
            profile_name = profile.get("name", "") or profile.get("pseudonym", "")
            profile_image = profile.get("profileImage", "") or profile.get("profileImageOptimized", "")
            profile_username = profile.get("xUsername", "") or profile.get("pseudonym", "")

        # ── PnL oficial del leaderboard ──
        official_pnl = None
        official_volume = None
        official_rank = None
        if leaderboard and isinstance(leaderboard, dict):
            official_pnl = leaderboard.get("pnl")
            official_volume = leaderboard.get("vol")
            official_rank = leaderboard.get("rank")

        # ── Métricas desde posiciones ABIERTAS — usar cashPnl del API ──
        open_cash_pnl = 0
        open_total_bought = 0
        open_positions_list = []

        for p in positions:
            try:
                size = float(p.get("size", 0))
                if size < 0.01:
                    continue
                cash_pnl = float(p.get("cashPnl", 0))
                avg_price = float(p.get("avgPrice", 0))
                cur_price = float(p.get("curPrice", 0))
                initial_val = float(p.get("initialValue", 0))
                current_val = float(p.get("currentValue", 0))
                total_b = float(p.get("totalBought", 0))
                pct_pnl = float(p.get("percentPnl", 0))

                open_cash_pnl += cash_pnl
                open_total_bought += total_b

                open_positions_list.append({
                    "title": p.get("title", ""),
                    "slug": p.get("slug", ""),
                    "size": round(size, 2),
                    "avg_price": round(avg_price, 4),
                    "cur_price": round(cur_price, 4),
                    "initial_value": round(initial_val, 2),
                    "current_value": round(current_val, 2),
                    "cash_pnl": round(cash_pnl, 2),
                    "pnl_pct": round(pct_pnl, 2),
                    "end_date": p.get("endDate", ""),
                })
            except (ValueError, TypeError):
                continue

        # ── Métricas desde posiciones CERRADAS — realizedPnl = net profit ──
        closed_pnl = 0
        closed_total_bought = 0
        closed_wins = 0
        closed_losses = 0

        for cp in closed_positions:
            try:
                rpnl = float(cp.get("realizedPnl", 0))
                tb = float(cp.get("totalBought", 0))
                closed_pnl += rpnl
                closed_total_bought += tb
                if rpnl > 0.5:
                    closed_wins += 1
                elif rpnl < -0.5:
                    closed_losses += 1
            except (ValueError, TypeError):
                continue

        # ── Totales — PREFERIR PnL oficial del leaderboard ──
        calculated_pnl = open_cash_pnl + closed_pnl
        # Usar PnL oficial si disponible, sino el calculado
        total_pnl = round(official_pnl, 2) if official_pnl is not None else round(calculated_pnl, 2)
        unrealized_pnl = open_cash_pnl
        # Derivar realized del oficial: realized = total_pnl - unrealized
        if official_pnl is not None:
            realized_pnl = official_pnl - open_cash_pnl
        else:
            realized_pnl = closed_pnl

        # Volumen oficial como base de inversión (más preciso que sumar posiciones parciales)
        if official_volume and official_volume > 0:
            estimated_initial = round(official_volume, 2)
        else:
            total_invested = open_total_bought + closed_total_bought
            estimated_initial = total_invested if total_invested > 0 else max(portfolio_value - total_pnl, 0)

        # Total de mercados — PREFERIR /traded count oficial
        total_markets = traded_count if traded_count > 0 else (len(positions) + len(closed_positions))

        # Win rate solo de posiciones cerradas (muestra parcial si hay muchas)
        total_resolved = closed_wins + closed_losses
        win_rate = round(closed_wins / max(total_resolved, 1) * 100, 1)

        # ROI basado en PnL oficial / volumen oficial
        roi_pct = round(total_pnl / max(estimated_initial, 1) * 100, 2) if estimated_initial > 0 else 0

        # ── Fecha primer trade ──
        earliest_ts = None
        if closed_positions:
            for cp in reversed(closed_positions):
                ts = cp.get("timestamp")
                if ts:
                    try:
                        if isinstance(ts, (int, float)):
                            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                        else:
                            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                        if earliest_ts is None or dt < earliest_ts:
                            earliest_ts = dt
                        break
                    except Exception:
                        pass
        if earliest_ts is None and activity_recent:
            last_item = activity_recent[-1]
            ts_field = last_item.get("timestamp") or last_item.get("created_at") or last_item.get("createdAt")
            if ts_field:
                try:
                    if isinstance(ts_field, (int, float)):
                        earliest_ts = datetime.fromtimestamp(ts_field, tz=timezone.utc)
                    else:
                        earliest_ts = datetime.fromisoformat(str(ts_field).replace("Z", "+00:00"))
                except Exception:
                    pass

        first_trade_date = earliest_ts.isoformat() if earliest_ts else None
        days_active = None
        if earliest_ts:
            try:
                days_active = (datetime.now(timezone.utc) - earliest_ts).days
            except Exception:
                pass

        # Ordenar posiciones por valor
        open_positions_list.sort(key=lambda x: abs(x["current_value"]), reverse=True)

        # Actividad reciente agrupada
        recent_trades = _group_activity(activity_recent)

        return {
            "address": addr,
            "profile_name": profile_name,
            "profile_image": profile_image,
            "profile_username": profile_username,
            "portfolio_value": round(portfolio_value, 2),
            "estimated_initial_capital": round(estimated_initial, 2),
            "total_pnl": total_pnl,
            "official_pnl": round(official_pnl, 2) if official_pnl is not None else None,
            "calculated_pnl": round(calculated_pnl, 2),
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "official_volume": round(official_volume, 2) if official_volume is not None else None,
            "official_rank": official_rank,
            "roi_pct": roi_pct,
            "win_rate": win_rate,
            "wins": closed_wins,
            "losses": closed_losses,
            "closed_sampled": len(closed_positions),
            "total_trades": total_markets,
            "first_trade_date": first_trade_date,
            "days_active": days_active,
            "open_positions_count": len(open_positions_list),
            "open_positions": open_positions_list[:30],
            "recent_trades": recent_trades,
        }


def _group_activity(activity: list, max_groups: int = 25) -> list:
    """Agrupar transacciones on-chain por mercado+lado en ventana de 5 min."""
    groups = {}
    for a in activity:
        try:
            title = a.get("title", a.get("question", ""))
            side = (a.get("side", a.get("type", "")) or "").upper()
            ts_raw = a.get("timestamp") or a.get("created_at") or a.get("createdAt")
            size = float(a.get("size", a.get("amount", 0)))
            price = float(a.get("price", 0))

            # Ventana temporal de 5 min
            ts_val = 0
            if isinstance(ts_raw, (int, float)):
                ts_val = int(ts_raw)
            elif ts_raw:
                try:
                    ts_val = int(datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp())
                except Exception:
                    pass
            window = ts_val // 300  # 5 min buckets

            key = f"{title}|{side}|{window}"
            if key not in groups:
                groups[key] = {
                    "title": title, "side": side, "size": 0, "price_sum": 0,
                    "count": 0, "timestamp": ts_raw, "outcome": a.get("outcome", ""),
                }
            g = groups[key]
            g["size"] += size
            g["price_sum"] += price * size
            g["count"] += 1
        except Exception:
            continue

    # Formatear y ordenar por timestamp desc
    result = []
    for g in groups.values():
        avg_price = g["price_sum"] / max(g["size"], 0.01)
        result.append({
            "title": g["title"],
            "side": g["side"],
            "size": round(g["size"], 2),
            "price": round(avg_price, 4),
            "timestamp": g["timestamp"],
            "outcome": g["outcome"],
            "tx_count": g["count"],
        })

    result.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
    return result[:max_groups]


# ── Batch Wallet Scan (Job persistente en DB) ────────────────────────
_cancel_job_ids: set[int] = set()  # IDs de jobs que deben cancelarse

@router.post("/api/wallets/batch-scan")
async def start_batch_scan(request: Request):
    """Iniciar batch scan de wallets. El progreso se guarda en DB."""
    db = request.app.state.db

    # Verificar si hay job activo en DB
    active = await db.get_active_scan_job()
    if active:
        return {"status": "already_running", "source": active.get("source", ""),
                "scanned": active.get("scanned", 0), "total": active.get("total", 0),
                "job_id": active.get("id")}

    body = await request.json()
    source = body.get("source", "wallets")
    if source not in ("wallets", "top_wallets", "whales", "flagged"):
        return JSONResponse({"error": "Fuente inválida"}, status_code=400)

    uid = await get_user_id(request)
    addresses = await db.get_addresses_for_batch_scan(source, user_id=uid)

    if not addresses:
        return {"status": "empty", "total": 0, "message": "No hay wallets para analizar en esta fuente"}

    # Crear job persistente en DB
    job_id = await db.create_scan_job(source, len(addresses))

    # Lanzar task en background
    asyncio.create_task(_run_batch_scan(db, addresses, source, job_id))

    return {"status": "started", "source": source, "total": len(addresses), "job_id": job_id}


async def _run_batch_scan(db, addresses: list[dict], source: str, job_id: int):
    """Escanear wallets en background. Progreso persistido en DB."""
    sem = asyncio.Semaphore(3)  # Max 3 concurrentes
    scanned = 0
    errors = 0
    _cancelled = False
    _lock = asyncio.Lock()

    async def scan_one(wallet_info: dict):
        nonlocal scanned, errors, _cancelled
        if _cancelled or job_id in _cancel_job_ids:
            _cancelled = True
            return
        addr = wallet_info["address"]
        name = wallet_info.get("name", "")
        profile_image = wallet_info.get("profile_image", "")

        try:
            async with sem:
                result = await _scan_wallet_for_batch(addr)
                if result:
                    score = _calc_wallet_score(result)
                    await db.save_scan_result({
                        "address": addr,
                        "source": source,
                        "name": name,
                        "profile_image": profile_image,
                        "portfolio_value": result.get("portfolio_value", 0),
                        "estimated_initial": result.get("estimated_initial_capital", 0),
                        "total_pnl": result.get("total_pnl", 0),
                        "realized_pnl": result.get("realized_pnl", 0),
                        "roi_pct": result.get("roi_pct", 0),
                        "win_rate": result.get("win_rate", 0),
                        "wins": result.get("wins", 0),
                        "losses": result.get("losses", 0),
                        "total_trades": result.get("total_trades", 0),
                        "days_active": result.get("days_active", 0),
                        "open_positions": result.get("open_positions_count", 0),
                        "score": score,
                    })
                else:
                    async with _lock:
                        errors += 1
        except Exception as e:
            print(f"[BatchScan] Error escaneando {addr[:12]}: {e}", flush=True)
            async with _lock:
                errors += 1
        finally:
            async with _lock:
                scanned += 1
            # Actualizar progreso en DB cada 5 wallets o al terminar
            if scanned % 5 == 0 or scanned >= len(addresses):
                try:
                    await db.update_scan_job(job_id, scanned, errors, addr[:12] + "...")
                except Exception:
                    pass

    # Escanear todas en paralelo (limitado por semáforo)
    tasks = [scan_one(w) for w in addresses]
    await asyncio.gather(*tasks)

    # Marcar job como completado o cancelado en DB
    _cancel_job_ids.discard(job_id)
    if _cancelled:
        await db.cancel_scan_job(job_id, scanned, errors)
        print(f"[BatchScan] CANCELADO job #{job_id}: {scanned}/{len(addresses)} "
              f"wallets de {source}", flush=True)
    else:
        await db.finish_scan_job(job_id, scanned, errors)
        print(f"[BatchScan] Completado job #{job_id}: {scanned}/{len(addresses)} "
              f"wallets de {source} ({errors} errores)", flush=True)


async def _scan_wallet_for_batch(addr: str) -> dict | None:
    """Versión para batch: usa leaderboard + /traded + positions para datos reales."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # ── Fetch paralelo ──
            async def _val():
                try:
                    r = await client.get(f"{_POLY_DATA_API}/value", params={"user": addr})
                    if r.status_code == 200:
                        data = r.json()
                        if isinstance(data, list) and data:
                            return float(data[0].get("value", 0))
                        elif isinstance(data, dict):
                            return float(data.get("value", 0))
                except Exception:
                    pass
                return 0

            async def _lb():
                try:
                    r = await client.get(f"{_POLY_DATA_API}/v1/leaderboard",
                                         params={"user": addr, "period": "ALL", "order": "PNL"})
                    if r.status_code == 200:
                        data = r.json()
                        if isinstance(data, list) and data:
                            return data[0]
                except Exception:
                    pass
                return None

            async def _traded():
                try:
                    r = await client.get(f"{_POLY_DATA_API}/traded", params={"user": addr})
                    if r.status_code == 200:
                        data = r.json()
                        if isinstance(data, dict):
                            return int(data.get("traded", 0))
                except Exception:
                    pass
                return 0

            async def _pos():
                try:
                    r = await client.get(f"{_POLY_DATA_API}/positions",
                                         params={"user": addr, "sizeThreshold": 0.01, "limit": 500,
                                                 "sortBy": "CURRENT", "sortDirection": "DESC"})
                    if r.status_code == 200:
                        d = r.json()
                        return d if isinstance(d, list) else []
                except Exception:
                    pass
                return []

            async def _closed():
                closed = []
                try:
                    offset = 0
                    while offset < 2500:
                        r = await client.get(f"{_POLY_DATA_API}/closed-positions",
                                             params={"user": addr, "limit": 50, "offset": offset,
                                                     "sortBy": "TIMESTAMP", "sortDirection": "DESC"})
                        if r.status_code != 200:
                            break
                        d = r.json()
                        page = d if isinstance(d, list) else []
                        if not page:
                            break
                        closed.extend(page)
                        if len(page) < 50:
                            break
                        offset += 50
                except Exception:
                    pass
                return closed

            portfolio_value, leaderboard, traded_count, positions, closed_positions = await asyncio.gather(
                _val(), _lb(), _traded(), _pos(), _closed()
            )

            # PnL oficial y volumen del leaderboard
            official_pnl = None
            official_volume = None
            if leaderboard and isinstance(leaderboard, dict):
                official_pnl = leaderboard.get("pnl")
                official_volume = leaderboard.get("vol")

            # Métricas desde posiciones abiertas — usar cashPnl del API
            open_cash_pnl = 0
            open_total_bought = 0
            open_count = 0

            for p in positions:
                try:
                    size = float(p.get("size", 0))
                    if size < 0.01:
                        continue
                    open_cash_pnl += float(p.get("cashPnl", 0))
                    open_total_bought += float(p.get("totalBought", 0))
                    open_count += 1
                except (ValueError, TypeError):
                    continue

            # Métricas desde posiciones cerradas — realizedPnl = net profit
            closed_pnl = 0
            closed_total_bought = 0
            wins = 0
            losses = 0

            for cp in closed_positions:
                try:
                    rpnl = float(cp.get("realizedPnl", 0))
                    tb = float(cp.get("totalBought", 0))
                    closed_pnl += rpnl
                    closed_total_bought += tb
                    if rpnl > 0.5:
                        wins += 1
                    elif rpnl < -0.5:
                        losses += 1
                except (ValueError, TypeError):
                    continue

            # Totales — PREFERIR PnL oficial del leaderboard
            calculated_pnl = open_cash_pnl + closed_pnl
            total_pnl = round(official_pnl, 2) if official_pnl is not None else round(calculated_pnl, 2)
            # Derivar realized del oficial: realized = total_pnl - unrealized
            if official_pnl is not None:
                realized_pnl = round(official_pnl - open_cash_pnl, 2)
            else:
                realized_pnl = round(closed_pnl, 2)
            if official_volume and official_volume > 0:
                estimated_initial = round(official_volume, 2)
            else:
                total_invested = open_total_bought + closed_total_bought
                estimated_initial = total_invested if total_invested > 0 else max(portfolio_value - total_pnl, 0)
            total_markets = traded_count if traded_count > 0 else (len(positions) + len(closed_positions))
            total_resolved = wins + losses
            win_rate = round(wins / max(total_resolved, 1) * 100, 1)
            roi_pct = round(total_pnl / max(estimated_initial, 1) * 100, 2) if estimated_initial > 0 else 0

            # Fecha primer trade desde closed-positions
            days_active = 0
            if closed_positions:
                try:
                    ts = closed_positions[-1].get("timestamp")
                    if ts:
                        if isinstance(ts, (int, float)):
                            ft = datetime.fromtimestamp(ts, tz=timezone.utc)
                        else:
                            ft = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                        days_active = (datetime.now(timezone.utc) - ft).days
                except Exception:
                    pass

            return {
                "address": addr,
                "portfolio_value": round(portfolio_value, 2),
                "estimated_initial_capital": round(estimated_initial, 2),
                "total_pnl": total_pnl,
                "realized_pnl": realized_pnl,
                "official_pnl": round(official_pnl, 2) if official_pnl is not None else None,
                "roi_pct": roi_pct,
                "win_rate": win_rate,
                "wins": wins,
                "losses": losses,
                "closed_sampled": len(closed_positions),
                "total_trades": total_markets,
                "days_active": days_active,
                "open_positions_count": open_count,
            }
    except Exception as e:
        print(f"[BatchScan] Error scan {addr[:12]}: {e}", flush=True)
        return None


def _calc_wallet_score(result: dict) -> int:
    """Calcular score 1-5 basado en win_rate, PnL, trades, ROI."""
    score = 0
    wr = result.get("win_rate", 0)
    pnl = result.get("total_pnl", 0)
    trades = result.get("total_trades", 0)
    roi = result.get("roi_pct", 0)

    # Win rate
    if wr >= 70:
        score += 2
    elif wr >= 55:
        score += 1

    # PnL positivo
    if pnl >= 10000:
        score += 2
    elif pnl >= 1000:
        score += 1
    elif pnl > 0:
        score += 0.5

    # Volumen de trades (experiencia)
    if trades >= 100:
        score += 1
    elif trades >= 20:
        score += 0.5

    # ROI
    if roi >= 30:
        score += 1
    elif roi >= 10:
        score += 0.5

    return max(1, min(5, round(score)))


@router.get("/api/wallets/batch-scan/status")
async def get_batch_scan_status(request: Request):
    """Estado del batch scan desde DB (persiste entre recargas)."""
    db = request.app.state.db
    active = await db.get_active_scan_job()
    if active:
        # Detectar job zombie: si no se actualizó en 3+ minutos, está muerto
        from datetime import datetime, timezone, timedelta
        updated = active.get("updated_at")
        if updated:
            if isinstance(updated, str):
                try:
                    updated = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                except Exception:
                    updated = None
            if updated:
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - updated
                if age > timedelta(minutes=3):
                    # Job zombie — cancelar automáticamente
                    await db.cancel_scan_job(active["id"], active.get("scanned", 0), active.get("errors", 0))
                    print(f"[BatchScan] Job zombie #{active['id']} cancelado (sin actualizar hace {age})", flush=True)
                    return {"running": False, "source": active.get("source", ""),
                            "total": active.get("total", 0), "scanned": active.get("scanned", 0),
                            "errors": active.get("errors", 0), "current": "",
                            "job_id": active.get("id"), "status": "cancelled",
                            "message": f"Job #{active['id']} cancelado automáticamente (servidor reiniciado)"}
        return {"running": True, "source": active.get("source", ""),
                "total": active.get("total", 0), "scanned": active.get("scanned", 0),
                "errors": active.get("errors", 0), "current": active.get("current_wallet", ""),
                "job_id": active.get("id"), "started_at": active.get("started_at")}
    # Sin job activo — retornar último completado
    latest = await db.get_latest_scan_job()
    if latest:
        return {"running": False, "source": latest.get("source", ""),
                "total": latest.get("total", 0), "scanned": latest.get("scanned", 0),
                "errors": latest.get("errors", 0), "current": "",
                "job_id": latest.get("id"), "status": latest.get("status", ""),
                "started_at": latest.get("started_at"), "finished_at": latest.get("finished_at")}
    return {"running": False, "total": 0, "scanned": 0}


@router.get("/api/wallets/batch-scan/results")
async def get_batch_scan_results(request: Request, source: str = "",
                                  sort_by: str = "total_pnl", sort_dir: str = "DESC",
                                  limit: int = 500, offset: int = 0,
                                  # Filtros de rango
                                  pnl_min: float = None, pnl_max: float = None,
                                  portfolio_min: float = None, portfolio_max: float = None,
                                  roi_min: float = None, roi_max: float = None,
                                  wr_min: float = None, wr_max: float = None,
                                  score_min: float = None, score_max: float = None,
                                  trades_min: float = None, trades_max: float = None,
                                  capital_min: float = None, capital_max: float = None,
                                  days_min: float = None, days_max: float = None,
                                  positions_min: float = None, positions_max: float = None):
    """Resultados con filtros avanzados. Todos los filtros son opcionales y acumulativos."""
    db = request.app.state.db
    filters = {}
    for key, val in [("pnl_min", pnl_min), ("pnl_max", pnl_max),
                     ("portfolio_min", portfolio_min), ("portfolio_max", portfolio_max),
                     ("roi_min", roi_min), ("roi_max", roi_max),
                     ("wr_min", wr_min), ("wr_max", wr_max),
                     ("score_min", score_min), ("score_max", score_max),
                     ("trades_min", trades_min), ("trades_max", trades_max),
                     ("capital_min", capital_min), ("capital_max", capital_max),
                     ("days_min", days_min), ("days_max", days_max),
                     ("positions_min", positions_min), ("positions_max", positions_max)]:
        if val is not None:
            filters[key] = val
    data = await db.get_scan_results(source=source, sort_by=sort_by, sort_dir=sort_dir,
                                      limit=limit, offset=offset, filters=filters)
    return data


@router.post("/api/wallets/batch-scan/cancel")
async def cancel_batch_scan(request: Request):
    """Cancelar el job de batch scan activo. Marca en DB + flag in-memory."""
    db = request.app.state.db
    active = await db.get_active_scan_job()
    if not active:
        return {"status": "no_active_job", "message": "No hay job activo para cancelar"}
    job_id = active["id"]
    _cancel_job_ids.add(job_id)
    # Marcar directamente en DB como cancelado (funciona incluso si el proceso murió)
    await db.cancel_scan_job(job_id, active.get("scanned", 0), active.get("errors", 0))
    return {"status": "cancelled", "job_id": job_id,
            "message": f"Job #{job_id} cancelado. {active.get('scanned', 0)} wallets procesadas."}


@router.delete("/api/wallets/batch-scan/clear")
async def clear_batch_scan_cache(request: Request):
    """Limpiar todos los resultados cacheados del batch scan."""
    db = request.app.state.db
    await db.clear_scan_results()
    return {"status": "ok", "message": "Cache de wallets limpiado"}


# ── Polymarket Data Proxies ──────────────────────────────────────────

@router.get("/api/polymarket/leaderboard")
async def poly_leaderboard(request: Request, category: str = "OVERALL",
                            period: str = "ALL", order: str = "PNL",
                            limit: int = 20, offset: int = 0, user: str = ""):
    """Proxy al leaderboard oficial de Polymarket."""
    params = {"category": category, "period": period, "order": order,
              "limit": min(limit, 50), "offset": offset}
    if user:
        params["user"] = user.strip().lower()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{_POLY_DATA_API}/v1/leaderboard", params=params)
            if r.status_code == 200:
                return r.json()
            return {"error": f"Leaderboard HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


@router.get("/api/polymarket/market-positions")
async def poly_market_positions(request: Request, market: str, status: str = "OPEN",
                                 sort_by: str = "TOKENS", sort_dir: str = "DESC",
                                 limit: int = 50, offset: int = 0):
    """Posiciones de todos los traders en un mercado específico."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{_POLY_DATA_API}/v1/market-positions",
                                 params={"market": market, "status": status,
                                         "sortBy": sort_by, "sortDirection": sort_dir,
                                         "limit": min(limit, 500), "offset": offset})
            if r.status_code == 200:
                return r.json()
            return {"error": f"Market positions HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


@router.get("/api/polymarket/price-history")
async def poly_price_history(request: Request, market: str,
                              interval: str = "1d", fidelity: int = 60):
    """Historial de precios de un mercado (conditionId)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{_POLY_CLOB_API}/prices-history",
                                 params={"market": market, "interval": interval,
                                         "fidelity": fidelity})
            if r.status_code == 200:
                return r.json()
            return {"error": f"Price history HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


@router.get("/api/polymarket/profile")
async def poly_profile(request: Request, address: str):
    """Perfil público de un trader desde Gamma API."""
    addr = address.strip().lower()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{_POLY_GAMMA_API}/public-profile",
                                 params={"address": addr})
            if r.status_code == 200:
                return r.json()
            return {"error": f"Profile HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


@router.get("/api/polymarket/holders")
async def poly_holders(request: Request, market: str, limit: int = 50):
    """Top holders de un mercado específico."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{_POLY_DATA_API}/holders",
                                 params={"market": market, "limit": min(limit, 100)})
            if r.status_code == 200:
                return r.json()
            return {"error": f"Holders HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


# ── v11: News Feed ────────────────────────────────────────────────────

@router.get("/api/news/feed")
async def get_news_feed(request: Request, hours: int = 24, market_id: str = "", limit: int = 100):
    db = request.app.state.db
    items = await db.get_news_feed(hours=hours, market_id=market_id, limit=limit)
    return {"items": items, "total": len(items)}


@router.get("/api/news/sentiment")
async def get_sentiments(request: Request, limit: int = 50):
    db = request.app.state.db
    data = await db.get_market_sentiments(limit=limit)
    return {"markets": data, "total": len(data)}


# ── v11: Insider Detection ───────────────────────────────────────────

@router.get("/api/insider/flags")
async def get_insider_flags(request: Request, hours: int = 48, min_prob: int = 15, limit: int = 100):
    db = request.app.state.db
    flags = await db.get_insider_flags(hours=hours, min_prob=min_prob, limit=limit)
    return {"flags": flags, "total": len(flags)}


# ── v11: Price Spikes ────────────────────────────────────────────────

@router.get("/api/spikes")
async def get_spikes(request: Request, hours: int = 24, min_pct: float = 0, limit: int = 100):
    db = request.app.state.db
    spikes = await db.get_price_spikes(hours=hours, min_pct=min_pct, limit=limit)
    return {"spikes": spikes, "total": len(spikes)}


# ── v11: Copy Trading ────────────────────────────────────────────────

@router.get("/api/copy-trading/targets")
async def get_copy_targets(request: Request):
    db = request.app.state.db
    uid = await get_user_id(request)
    targets = await db.get_watchlisted_wallets_detail()
    return {"targets": targets, "total": len(targets)}


@router.post("/api/copy-trading/targets/{wallet_address}/config")
async def save_wallet_copy_config(request: Request, wallet_address: str):
    """Guardar configuración de copy trade para una wallet específica."""
    db = request.app.state.db
    body = await request.json()
    ok = await db.save_wallet_copy_config(wallet_address, body)
    return {"ok": ok}


@router.get("/api/copy-trading/targets/{wallet_address}/config")
async def get_wallet_copy_config(request: Request, wallet_address: str):
    """Obtener configuración de copy trade de una wallet."""
    db = request.app.state.db
    config = await db.get_wallet_copy_config(wallet_address)
    return config or {}


@router.post("/api/copy-trading/targets/{wallet_address}/reset-budget")
async def reset_wallet_budget(request: Request, wallet_address: str):
    """Resetear presupuesto usado de una wallet."""
    db = request.app.state.db
    await db.reset_wallet_budget(wallet_address)
    return {"ok": True}


@router.get("/api/copy-trading/trades")
async def get_copy_trades(request: Request, limit: int = 100):
    db = request.app.state.db
    uid = await get_user_id(request)
    trades = await db.get_real_copy_trades(user_id=uid, limit=limit)
    return {"trades": trades, "total": len(trades)}


@router.get("/api/copy-trading/stats")
async def get_copy_stats(request: Request):
    db = request.app.state.db
    uid = await get_user_id(request)
    return await db.get_real_copy_trading_stats(user_id=uid)


# ── v11: AI Analysis ────────────────────────────────────────────────

@router.get("/api/ai/analyses")
async def get_ai_analyses(request: Request, hours: int = 24, min_edge: float = 0, limit: int = 50):
    db = request.app.state.db
    data = await db.get_ai_analyses(hours=hours, min_edge=min_edge, limit=limit)
    return {"analyses": data, "total": len(data)}


@router.post("/api/ai/analyze/{market_id}")
async def analyze_market_ai(request: Request, market_id: str):
    bot = request.app.state.bot
    if not bot or not bot.market_agent or not bot.market_agent.enabled:
        return {"error": "AI Agent no configurado. Configura OPENAI_API_KEY en las variables de entorno."}
    db = request.app.state.db
    # Obtener datos del mercado
    market_data = None
    try:
        markets = await db.get_tracked_markets(limit=500)
        market_data = next((m for m in markets if m.get("condition_id") == market_id), None)
    except Exception:
        pass
    if not market_data:
        market_data = {"condition_id": market_id, "question": "", "price": 0.5, "volume": 0}
    # Obtener noticias
    news = await db.get_news_feed(hours=24, market_id=market_id, limit=5)
    result = await bot.market_agent.analyze_market(market_data, news=news)
    return result


# ── v11: Spread Analysis ────────────────────────────────────────────

@router.get("/api/spreads")
async def get_spreads(request: Request, limit: int = 50):
    db = request.app.state.db
    data = await db.get_spread_opportunities(limit=limit)
    return {"markets": data, "total": len(data)}


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
    # Whale Tracker
    whale_tracker_min_size: int | None = None
    whale_tracker_enabled: bool | None = None
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
    "whale_tracker_min_size": ("whale_tracker_min_size", "WHALE_TRACKER_MIN_SIZE"),
    "whale_tracker_enabled": ("whale_tracker_enabled", "WHALE_TRACKER_ENABLED"),
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
        # Whale Tracker
        "whale_tracker_min_size": _v("whale_tracker_min_size", config.WHALE_TRACKER_MIN_SIZE),
        "whale_tracker_enabled": _v("whale_tracker_enabled", config.WHALE_TRACKER_ENABLED),
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
        "weather_arb": config.FEATURE_WEATHER_ARB,
    }


class FeaturesUpdate(BaseModel):
    orderbook_depth: bool | None = None
    market_classification: bool | None = None
    wallet_baskets: bool | None = None
    sniper_dbscan: bool | None = None
    crypto_arb: bool | None = None
    weather_arb: bool | None = None


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
    if body.weather_arb is not None:
        if uid == 1: config.FEATURE_WEATHER_ARB = body.weather_arb
        updated["weather_arb"] = body.weather_arb
        data["feature_weather_arb"] = str(body.weather_arb)
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


@router.get("/api/crypto-arb/watching")
async def crypto_watching_markets(request: Request):
    """Mercados en observación del early entry detector."""
    bot = request.app.state.bot
    if bot and getattr(bot, 'early_detector', None):
        return bot.early_detector.get_watching_markets()
    return []


@router.get("/api/crypto-arb/live-signals")
async def crypto_live_signals(request: Request, limit: int = 50):
    """Señales en vivo combinando score + early entry detectors."""
    bot = request.app.state.bot
    signals = []
    if bot and getattr(bot, 'crypto_detector', None):
        signals = bot.crypto_detector.get_recent_signals(limit)
    if bot and getattr(bot, 'early_detector', None):
        early = bot.early_detector.get_recent_signals(limit)
        existing_cids = {s["condition_id"] for s in signals}
        for es in early:
            if es["condition_id"] not in existing_cids:
                signals.append(es)
                existing_cids.add(es["condition_id"])
    signals.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return signals[:limit]


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


@router.post("/api/crypto-arb/autotrades/reset")
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
            for table in ["alerts", "autotrades", "alert_autotrades", "weather_trades"]:
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
    """Señales en vivo y mercados activos — merge score + early entry."""
    bot = request.app.state.bot
    if not bot or not hasattr(bot, "crypto_detector") or not bot.crypto_detector:
        return {"signals": [], "markets": [], "enabled": False}
    signals = bot.crypto_detector.get_recent_signals(50)
    # Merge señales early entry
    if getattr(bot, 'early_detector', None):
        early = bot.early_detector.get_recent_signals(50)
        if early:
            existing_cids = {s["condition_id"] for s in signals}
            for es in early:
                if es["condition_id"] not in existing_cids:
                    signals.append(es)
                    existing_cids.add(es["condition_id"])
    signals.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return {
        "signals": signals[:100],
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
        "sniper_min_move_pct": _v("crypto_sniper_min_move", config.CRYPTO_SNIPER_MIN_MOVE_PCT),
        "sniper_entry_delay_sec": _v("crypto_sniper_entry_delay", config.CRYPTO_SNIPER_ENTRY_DELAY_SEC),
        "sniper_entry_max_sec": _v("crypto_sniper_entry_max", config.CRYPTO_SNIPER_ENTRY_MAX_SEC),
        "sniper_max_buy_price": _v("crypto_sniper_max_buy_price", config.CRYPTO_SNIPER_MAX_BUY_PRICE),
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
    sniper_min_move_pct: float | None = None
    sniper_entry_delay_sec: int | None = None
    sniper_entry_max_sec: int | None = None
    sniper_max_buy_price: float | None = None


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
    if body.strategy is not None and body.strategy in ("divergence", "score", "sniper"):
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
    if body.sniper_min_move_pct is not None:
        if uid == 1: config.CRYPTO_SNIPER_MIN_MOVE_PCT = body.sniper_min_move_pct
        updated["sniper_min_move_pct"] = body.sniper_min_move_pct
        data["crypto_sniper_min_move"] = str(body.sniper_min_move_pct)
    if body.sniper_entry_delay_sec is not None:
        if uid == 1: config.CRYPTO_SNIPER_ENTRY_DELAY_SEC = body.sniper_entry_delay_sec
        updated["sniper_entry_delay_sec"] = body.sniper_entry_delay_sec
        data["crypto_sniper_entry_delay"] = str(body.sniper_entry_delay_sec)
    if body.sniper_entry_max_sec is not None:
        if uid == 1: config.CRYPTO_SNIPER_ENTRY_MAX_SEC = body.sniper_entry_max_sec
        updated["sniper_entry_max_sec"] = body.sniper_entry_max_sec
        data["crypto_sniper_entry_max"] = str(body.sniper_entry_max_sec)
    if body.sniper_max_buy_price is not None:
        if uid == 1: config.CRYPTO_SNIPER_MAX_BUY_PRICE = body.sniper_max_buy_price
        updated["sniper_max_buy_price"] = body.sniper_max_buy_price
        data["crypto_sniper_max_buy_price"] = str(body.sniper_max_buy_price)
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
        "at_funder_address", "at_min_score",
        "at_use_score_strategy", "at_use_early_entry", "at_early_entry_bet_size",
        "at_early_entry_enabled", "at_early_entry_pre_monitor",
        "at_early_entry_window", "at_early_entry_min_momentum",
        "at_early_entry_min_momentum_5m", "at_early_entry_min_momentum_15m",
        "at_ee_take_profit_enabled", "at_ee_take_profit_pct",
        "at_stop_loss_enabled", "at_stop_loss_pct", "at_take_profit_pct",
        "at_max_holding_sec", "at_trailing_stop_enabled", "at_trailing_stop_pct",
        "at_slippage_max_pct",
        # Sniper
        "at_use_sniper", "at_sniper_bet_size", "at_sniper_slippage_pct",
        "at_sniper_min_move_pct", "at_sniper_max_buy_price", "at_sniper_use_live_price",
        "at_sniper_cooldown_sec", "at_sniper_entry_delay_sec",
        "at_sniper_entry_max_sec", "at_sniper_tp_enabled", "at_sniper_tp_pct",
        "at_sniper_gtc_timeout_sec",
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
        # Sniper
        "use_sniper": raw.get("at_use_sniper", "false") == "true",
        "sniper_bet_size": float(raw.get("at_sniper_bet_size", 3)),
        "sniper_slippage_pct": float(raw.get("at_sniper_slippage_pct", 5)),
        "sniper_min_move_pct": float(raw.get("at_sniper_min_move_pct", 0.03)),
        "sniper_max_buy_price": float(raw.get("at_sniper_max_buy_price", 0.60)),
        "sniper_use_live_price": raw.get("at_sniper_use_live_price") == "true",
        "sniper_cooldown_sec": int(raw.get("at_sniper_cooldown_sec", 0)),
        "sniper_entry_delay_sec": int(raw.get("at_sniper_entry_delay_sec", 55)),
        "sniper_entry_max_sec": int(raw.get("at_sniper_entry_max_sec", 150)),
        "sniper_tp_enabled": raw.get("at_sniper_tp_enabled") == "true",
        "sniper_tp_pct": float(raw.get("at_sniper_tp_pct", 30)),
        "sniper_gtc_timeout_sec": int(raw.get("at_sniper_gtc_timeout_sec", 40)),
        "min_score": float(raw.get("at_min_score", 0)),
        "use_score_strategy": raw.get("at_use_score_strategy", "true") == "true",
        "use_early_entry": raw.get("at_use_early_entry", "false") == "true",
        "early_entry_bet_size": float(raw.get("at_early_entry_bet_size", 3)),
        "early_entry_enabled": raw.get("at_early_entry_enabled") == "true",
        "early_entry_pre_monitor": int(raw.get("at_early_entry_pre_monitor", 120)),
        "early_entry_window": int(raw.get("at_early_entry_window", 15)),
        "early_entry_min_momentum": float(raw.get("at_early_entry_min_momentum", 0.03)),
        "early_entry_min_momentum_5m": float(raw.get("at_early_entry_min_momentum_5m", raw.get("at_early_entry_min_momentum", 0.03))),
        "early_entry_min_momentum_15m": float(raw.get("at_early_entry_min_momentum_15m", 0.05)),
        "ee_take_profit_enabled": raw.get("at_ee_take_profit_enabled") == "true",
        "ee_take_profit_pct": float(raw.get("at_ee_take_profit_pct", 40)),
        # Maker Orders
        "maker_spread_offset": float(raw.get("at_maker_spread_offset", 0.02)),
        "maker_max_open_orders": int(raw.get("at_maker_max_open_orders", 10)),
        "maker_requote_threshold": float(raw.get("at_maker_requote_threshold", 0.03)),
        "maker_fill_timeout_sec": int(raw.get("at_maker_fill_timeout_sec", 120)),
        "hybrid_score_threshold": float(raw.get("at_hybrid_score_threshold", 0.50)),
        # Maker stats
        "maker_open_orders": len(autotrader._open_maker_orders) if autotrader else 0,
        "maker_stats": autotrader._maker_stats if autotrader else {},
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
    if "min_score" in body:
        data["at_min_score"] = str(body["min_score"])
    # Strategy selection
    if "use_score_strategy" in body:
        data["at_use_score_strategy"] = "true" if body["use_score_strategy"] else "false"
    if "use_early_entry" in body:
        data["at_use_early_entry"] = "true" if body["use_early_entry"] else "false"
    if "early_entry_bet_size" in body:
        data["at_early_entry_bet_size"] = str(body["early_entry_bet_size"])
    # Early Entry detector config
    if "early_entry_enabled" in body:
        data["at_early_entry_enabled"] = "true" if body["early_entry_enabled"] else "false"
    if "early_entry_pre_monitor" in body:
        data["at_early_entry_pre_monitor"] = str(body["early_entry_pre_monitor"])
    if "early_entry_window" in body:
        data["at_early_entry_window"] = str(body["early_entry_window"])
    if "early_entry_min_momentum" in body:
        data["at_early_entry_min_momentum"] = str(body["early_entry_min_momentum"])
    if "early_entry_min_momentum_5m" in body:
        data["at_early_entry_min_momentum_5m"] = str(body["early_entry_min_momentum_5m"])
    if "early_entry_min_momentum_15m" in body:
        data["at_early_entry_min_momentum_15m"] = str(body["early_entry_min_momentum_15m"])
    if "ee_take_profit_enabled" in body:
        data["at_ee_take_profit_enabled"] = "true" if body["ee_take_profit_enabled"] else "false"
    if "ee_take_profit_pct" in body:
        data["at_ee_take_profit_pct"] = str(body["ee_take_profit_pct"])
    # Sniper config
    if "use_sniper" in body:
        data["at_use_sniper"] = "true" if body["use_sniper"] else "false"
    if "sniper_bet_size" in body:
        data["at_sniper_bet_size"] = str(body["sniper_bet_size"])
    if "sniper_slippage_pct" in body:
        data["at_sniper_slippage_pct"] = str(body["sniper_slippage_pct"])
    if "sniper_min_move_pct" in body:
        data["at_sniper_min_move_pct"] = str(body["sniper_min_move_pct"])
    if "sniper_max_buy_price" in body:
        data["at_sniper_max_buy_price"] = str(body["sniper_max_buy_price"])
    if "sniper_use_live_price" in body:
        data["at_sniper_use_live_price"] = "true" if body["sniper_use_live_price"] else "false"
    if "sniper_cooldown_sec" in body:
        data["at_sniper_cooldown_sec"] = str(body["sniper_cooldown_sec"])
    if "sniper_entry_delay_sec" in body:
        data["at_sniper_entry_delay_sec"] = str(body["sniper_entry_delay_sec"])
    if "sniper_entry_max_sec" in body:
        data["at_sniper_entry_max_sec"] = str(body["sniper_entry_max_sec"])
    if "sniper_tp_enabled" in body:
        data["at_sniper_tp_enabled"] = "true" if body["sniper_tp_enabled"] else "false"
    if "sniper_tp_pct" in body:
        data["at_sniper_tp_pct"] = str(body["sniper_tp_pct"])
    if "sniper_gtc_timeout_sec" in body:
        data["at_sniper_gtc_timeout_sec"] = str(body["sniper_gtc_timeout_sec"])
    # Maker Orders config
    if "maker_spread_offset" in body:
        data["at_maker_spread_offset"] = str(body["maker_spread_offset"])
    if "maker_max_open_orders" in body:
        data["at_maker_max_open_orders"] = str(body["maker_max_open_orders"])
    if "maker_requote_threshold" in body:
        data["at_maker_requote_threshold"] = str(body["maker_requote_threshold"])
    if "maker_fill_timeout_sec" in body:
        data["at_maker_fill_timeout_sec"] = str(body["maker_fill_timeout_sec"])
    if "hybrid_score_threshold" in body:
        data["at_hybrid_score_threshold"] = str(body["hybrid_score_threshold"])
    if data:
        await db.set_config_bulk(data, user_id=uid)
    # Recargar config en el autotrader
    autotrader = getattr(request.app.state, 'autotrader', None)
    if autotrader:
        try:
            await autotrader.reload_config(user_id=uid)
        except Exception:
            pass
    # Recargar config en el early detector
    bot = getattr(request.app.state, 'bot', None)
    if bot and getattr(bot, 'early_detector', None):
        try:
            await bot._configure_early_detector()
        except Exception:
            pass
    # Recargar config sniper en el detector
    if bot and getattr(bot, '_configure_sniper_detector', None):
        try:
            await bot._configure_sniper_detector()
        except Exception:
            pass
    return {"status": "ok"}


# ── Paper Trading Maker ─────────────────────────────────────────

@router.get("/api/crypto-arb/paper-trading")
async def get_paper_trading(request: Request):
    """Obtener estado completo del paper trader maker."""
    bot = getattr(request.app.state, 'bot', None)
    pt = bot.paper_trader if bot else None
    if not pt:
        return {"enabled": False, "status": {}, "open_orders": [], "resolved": []}
    return {
        "enabled": pt._enabled,
        "status": pt.get_status(),
        "open_orders": pt.get_open_orders(),
        "resolved": pt.get_resolved_orders(100),
    }


@router.post("/api/crypto-arb/paper-trading/config")
async def save_paper_trading_config(request: Request):
    """Guardar configuración del paper trader maker."""
    bot = getattr(request.app.state, 'bot', None)
    pt = bot.paper_trader if bot else None
    if not pt:
        return {"status": "error", "error": "Paper trader no disponible"}
    body = await request.json()
    pt.configure(body)
    # Persistir en DB
    db = request.app.state.db
    uid = await get_user_id(request)
    data = {}
    if "enabled" in body:
        data["paper_trading_enabled"] = "true" if body["enabled"] else "false"
    if "bet_size" in body:
        data["paper_bet_size"] = str(body["bet_size"])
    if "spread_offset" in body:
        data["paper_spread_offset"] = str(body["spread_offset"])
    if "initial_capital" in body:
        data["paper_initial_capital"] = str(body["initial_capital"])
    if "mode" in body:
        data["paper_mode"] = body["mode"]
    if data:
        await db.set_config_bulk(data, user_id=uid)
    return {"status": "ok"}


@router.post("/api/crypto-arb/paper-trading/reset")
async def reset_paper_trading(request: Request):
    """Resetear estado del paper trader."""
    bot = getattr(request.app.state, 'bot', None)
    pt = bot.paper_trader if bot else None
    if not pt:
        return {"status": "error", "error": "Paper trader no disponible"}
    pt.reset()
    return {"status": "ok"}


# ── Market Maker Bilateral ──────────────────────────────────────

@router.get("/api/market-maker/status")
async def get_market_maker_status(request: Request):
    """Estado completo del market maker bilateral."""
    bot = getattr(request.app.state, 'bot', None)
    mm = getattr(bot, 'crypto_market_maker', None)
    if not mm:
        return {"enabled": False, "status": {}, "open_orders": [], "inventory": []}
    return {
        "status": mm.get_status(),
        "open_orders": mm.get_open_orders(),
        "inventory": mm.get_inventory(),
        "resolved": mm.get_resolved(50),
    }


@router.get("/api/market-maker/config")
async def get_market_maker_config(request: Request):
    """Obtener config del market maker desde DB."""
    db = request.app.state.db
    uid = await get_user_id(request)
    raw = await db.get_config_bulk([
        "mm_enabled", "mm_bet_size", "mm_spread_target",
        "mm_max_inventory", "mm_max_markets", "mm_requote_sec",
        "mm_requote_threshold", "mm_fill_timeout_sec",
        "mm_bias_enabled", "mm_bias_strength",
        "mm_paper_mode", "mm_max_daily_loss",
        "mm_min_time_remaining_sec", "mm_rebate_rate",
    ], user_id=uid)
    return {"config": raw}


@router.post("/api/market-maker/config")
async def save_market_maker_config(request: Request):
    """Guardar config del market maker y recargar."""
    bot = getattr(request.app.state, 'bot', None)
    mm = getattr(bot, 'crypto_market_maker', None)
    db = request.app.state.db
    uid = await get_user_id(request)
    body = await request.json()

    # Mapear keys del body a keys de DB
    key_map = {
        "enabled": "mm_enabled",
        "bet_size": "mm_bet_size",
        "spread_target": "mm_spread_target",
        "max_inventory": "mm_max_inventory",
        "max_markets": "mm_max_markets",
        "requote_sec": "mm_requote_sec",
        "requote_threshold": "mm_requote_threshold",
        "fill_timeout_sec": "mm_fill_timeout_sec",
        "bias_enabled": "mm_bias_enabled",
        "bias_strength": "mm_bias_strength",
        "paper_mode": "mm_paper_mode",
        "max_daily_loss": "mm_max_daily_loss",
        "min_time_remaining_sec": "mm_min_time_remaining_sec",
        "rebate_rate": "mm_rebate_rate",
    }
    data = {}
    for body_key, db_key in key_map.items():
        if body_key in body:
            val = body[body_key]
            if isinstance(val, bool):
                data[db_key] = "true" if val else "false"
            else:
                data[db_key] = str(val)
    if data:
        await db.set_config_bulk(data, user_id=uid)
    if mm:
        await mm.reload_config()
    return {"status": "ok"}


@router.get("/api/market-maker/trades")
async def get_market_maker_trades(request: Request):
    """Historial de trades bilaterales del market maker."""
    db = request.app.state.db
    uid = await get_user_id(request)
    trades = await db.get_mm_trades(hours=48, user_id=uid)
    stats = await db.get_mm_stats(user_id=uid)
    return {"trades": trades, "stats": stats}


@router.post("/api/market-maker/reset")
async def reset_market_maker(request: Request):
    """Resetear estado del market maker."""
    bot = getattr(request.app.state, 'bot', None)
    mm = getattr(bot, 'crypto_market_maker', None)
    if not mm:
        return {"status": "error", "error": "Market maker no disponible"}
    mm.reset()
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
        "aat_api_key", "aat_private_key", "aat_funder_address",
        # v10
        "aat_orderbook_check_enabled", "aat_max_price_impact_pct", "aat_buy_price_bump",
        "aat_dca_enabled", "aat_dca_splits", "aat_dca_interval_sec",
        "aat_volume_spike_boost", "aat_smart_watchlist_boost", "aat_funding_chain_boost",
        "aat_category_scoring_enabled",
        # Copy Trade
        "aat_copy_trade_enabled", "aat_copy_trade_bet_size",
        "aat_copy_trade_max_positions", "aat_copy_trade_max_daily",
        "aat_copy_trade_slippage",
        "arb_complement_enabled", "arb_complement_min_edge",
        "arb_complement_scan_interval", "arb_complement_max_markets",
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
        "funder_address": raw.get("aat_funder_address", ""),
        # v10
        "orderbook_check_enabled": raw.get("aat_orderbook_check_enabled") == "true",
        "max_price_impact_pct": float(raw.get("aat_max_price_impact_pct", 3)),
        "buy_price_bump": float(raw.get("aat_buy_price_bump", 0.02)),
        "dca_enabled": raw.get("aat_dca_enabled") == "true",
        "dca_splits": int(raw.get("aat_dca_splits", 2)),
        "dca_interval_sec": int(raw.get("aat_dca_interval_sec", 30)),
        "volume_spike_boost": int(raw.get("aat_volume_spike_boost", 3)),
        "smart_watchlist_boost": int(raw.get("aat_smart_watchlist_boost", 3)),
        "funding_chain_boost": int(raw.get("aat_funding_chain_boost", 2)),
        "category_scoring_enabled": raw.get("aat_category_scoring_enabled") == "true",
        # Copy Trade
        "copy_trade_enabled": raw.get("aat_copy_trade_enabled") == "true",
        "copy_trade_bet_size": float(raw.get("aat_copy_trade_bet_size", 10)),
        "copy_trade_max_positions": int(raw.get("aat_copy_trade_max_positions", 3)),
        "copy_trade_max_daily": int(raw.get("aat_copy_trade_max_daily", 10)),
        "copy_trade_slippage": float(raw.get("aat_copy_trade_slippage", 3)),
        "complement_arb_enabled": raw.get("arb_complement_enabled") == "true",
        "complement_arb_min_edge": float(raw.get("arb_complement_min_edge", 1)),
        "complement_arb_scan_interval": int(raw.get("arb_complement_scan_interval", 60)),
        "complement_arb_max_markets": int(raw.get("arb_complement_max_markets", 100)),
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
    # Funder address
    if "funder_address" in body:
        data["aat_funder_address"] = str(body["funder_address"]).strip()
    # v10: Features avanzadas
    if "orderbook_check_enabled" in body:
        data["aat_orderbook_check_enabled"] = "true" if body["orderbook_check_enabled"] else "false"
    if "max_price_impact_pct" in body:
        data["aat_max_price_impact_pct"] = str(body["max_price_impact_pct"])
    if "buy_price_bump" in body:
        data["aat_buy_price_bump"] = str(body["buy_price_bump"])
    if "dca_enabled" in body:
        data["aat_dca_enabled"] = "true" if body["dca_enabled"] else "false"
    if "dca_splits" in body:
        data["aat_dca_splits"] = str(body["dca_splits"])
    if "dca_interval_sec" in body:
        data["aat_dca_interval_sec"] = str(body["dca_interval_sec"])
    if "volume_spike_boost" in body:
        data["aat_volume_spike_boost"] = str(body["volume_spike_boost"])
    if "smart_watchlist_boost" in body:
        data["aat_smart_watchlist_boost"] = str(body["smart_watchlist_boost"])
    if "funding_chain_boost" in body:
        data["aat_funding_chain_boost"] = str(body["funding_chain_boost"])
    if "category_scoring_enabled" in body:
        data["aat_category_scoring_enabled"] = "true" if body["category_scoring_enabled"] else "false"
    # Copy Trade
    if "copy_trade_enabled" in body:
        data["aat_copy_trade_enabled"] = "true" if body["copy_trade_enabled"] else "false"
    if "copy_trade_bet_size" in body:
        data["aat_copy_trade_bet_size"] = str(body["copy_trade_bet_size"])
    if "copy_trade_max_positions" in body:
        data["aat_copy_trade_max_positions"] = str(body["copy_trade_max_positions"])
    if "copy_trade_max_daily" in body:
        data["aat_copy_trade_max_daily"] = str(body["copy_trade_max_daily"])
    if "copy_trade_slippage" in body:
        data["aat_copy_trade_slippage"] = str(body["copy_trade_slippage"])
    # v10: Complement Arb
    if "complement_arb_enabled" in body:
        data["arb_complement_enabled"] = "true" if body["complement_arb_enabled"] else "false"
    if "complement_arb_min_edge" in body:
        data["arb_complement_min_edge"] = str(body["complement_arb_min_edge"])
    if "complement_arb_scan_interval" in body:
        data["arb_complement_scan_interval"] = str(body["complement_arb_scan_interval"])
    if "complement_arb_max_markets" in body:
        data["arb_complement_max_markets"] = str(body["complement_arb_max_markets"])
    if data:
        await db.set_config_bulk(data, user_id=uid)
    # Recargar config en alert autotrader y complement arb (solo user 1 retrocompat)
    if uid == 1:
        aat = getattr(request.app.state, 'alert_autotrader', None)
        if aat:
            try:
                await aat.reload_config()
            except Exception:
                pass
        carb = getattr(request.app.state, 'complement_arb', None)
        if carb:
            try:
                await carb.initialize()
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


# ── v10: Backtester ─────────────────────────────────────────────────

@router.post("/api/backtest")
async def run_backtest(request: Request):
    """Ejecutar backtest de alertas con parámetros personalizados."""
    user_id = await get_user_id(request)
    db = request.app.state.db
    body = await request.json()

    from src.strategies.alert_backtester import AlertBacktester
    backtester = AlertBacktester(db)
    result = await backtester.run_backtest(body)
    return result


# ── v10: Complement Arb ─────────────────────────────────────────────

@router.get("/api/complement-arb")
async def get_complement_arb(request: Request):
    """Obtener oportunidades de arbitraje binario (YES+NO < $1)."""
    scanner = getattr(request.app.state, "complement_arb", None)
    if not scanner:
        return {"enabled": False, "opportunities": [], "error": "Scanner no inicializado"}
    return scanner.get_status()


@router.post("/api/complement-arb/scan")
async def trigger_complement_scan(request: Request):
    """Forzar un scan de complement arb."""
    scanner = getattr(request.app.state, "complement_arb", None)
    if not scanner:
        return {"error": "Scanner no inicializado"}
    opportunities = await scanner.scan(force=True)
    return {"opportunities": len(opportunities), "results": scanner.get_status()}


# ── v10: Alert PnL Tracking ─────────────────────────────────────────

@router.get("/api/alert-pnl")
async def get_alert_pnl(request: Request):
    """Paper Trading PnL — trackea todas las alertas BUY como posiciones virtuales.
    PnL se calcula por resolución del mercado o por wallet exit (SELL detectado)."""
    user_id = await get_user_id(request)
    db = request.app.state.db

    # Estadísticas generales de paper trading
    stats = await db.get_paper_trading_stats(user_id)

    # Historial de PnL por día (para gráfico de curva)
    pnl_history = await db.get_paper_pnl_history(days=60, user_id=user_id)

    # Ranking de wallets alertadas por profit
    wallet_ranking = await db.get_paper_wallet_ranking(limit=15, user_id=user_id)

    # Posiciones abiertas (alertas BUY sin resolver ni cerrar)
    open_trades = await db.get_open_paper_alerts(user_id)

    # Trades cerrados recientes
    recent_trades = await db.get_paper_recent_trades(limit=50, user_id=user_id)

    # PnL por rango de score (análisis de calidad de señales)
    pnl_by_score = await db.get_paper_pnl_by_score(user_id)

    return {
        "stats": stats,
        "pnl_history": pnl_history,
        "wallet_ranking": wallet_ranking,
        "open_trades": open_trades,
        "recent_trades": recent_trades,
        "pnl_by_score": pnl_by_score,
    }


@router.post("/api/alert-pnl/reset-prices")
async def reset_alert_prices(request: Request):
    """Forzar refresh de price_latest en todas las alertas abiertas."""
    db = request.app.state.db
    count = await db.reset_all_price_latest()
    return {"reset": count, "message": f"Se resetearon {count} alertas. Los precios se actualizarán en el próximo ciclo (~10 min)."}


# ── v10: Volume & Market Intelligence ────────────────────────────────

@router.get("/api/volume-spikes")
async def get_volume_spikes(request: Request):
    """Obtener mercados con volume spikes activos."""
    aat = getattr(request.app.state, "alert_autotrader", None)
    if not aat:
        return {"spikes": []}

    spikes = []
    for market_id, entries in aat._volume_tracker.items():
        if len(entries) < 3:
            continue
        ratio = aat.get_volume_spike_ratio(market_id)
        if ratio >= 2.0:
            bias = aat.get_volume_direction_bias(market_id)
            total_vol = sum(e["volume"] for e in entries)
            spikes.append({
                "market_id": market_id,
                "spike_ratio": round(ratio, 1),
                "direction_bias": round(bias, 1),
                "total_volume_4h": round(total_vol, 0),
                "trade_count": len(entries),
            })

    spikes.sort(key=lambda x: -x["spike_ratio"])
    return {"spikes": spikes[:20]}


@router.get("/api/smart-watchlist")
async def get_smart_watchlist(request: Request):
    """Obtener wallets en la smart watchlist."""
    db = request.app.state.db
    try:
        wallets = await db.get_watchlisted_wallets()
        # Obtener detalles de cada wallet
        details = []
        async with db._pool.acquire() as conn:
            for addr in list(wallets)[:30]:
                row = await conn.fetchrow(
                    "SELECT address, pseudonym, smart_money_score, win_count, loss_count, "
                    "total_pnl, roi_pct, total_volume FROM wallets WHERE address = $1",
                    addr
                )
                if row:
                    total = (row["win_count"] or 0) + (row["loss_count"] or 0)
                    details.append({
                        "address": row["address"],
                        "name": row["pseudonym"] or row["address"][:10] + "...",
                        "score": round(float(row["smart_money_score"] or 0), 1),
                        "win_rate": round((row["win_count"] or 0) / max(total, 1) * 100, 1),
                        "total_pnl": round(float(row["total_pnl"] or 0), 2),
                        "roi_pct": round(float(row["roi_pct"] or 0), 1),
                        "volume": round(float(row["total_volume"] or 0), 0),
                        "resolved": total,
                    })
        details.sort(key=lambda x: -x["score"])
        return {"count": len(details), "wallets": details}
    except Exception as e:
        return {"count": 0, "wallets": [], "error": str(e)}


@router.get("/api/category-edge")
async def get_category_edge(request: Request):
    """Obtener win rate por categoría para category-specific scoring."""
    db = request.app.state.db
    edges = await db.get_category_edge()
    return {"categories": edges}


# ── Weather Arb ──────────────────────────────────────────────────────

@router.get("/api/weather-arb/stats")
async def weather_arb_stats(request: Request):
    """Estadísticas completas del weather arb."""
    db = request.app.state.db
    uid = await get_user_id(request)
    try:
        db_stats = await db.get_weather_trade_stats(user_id=uid)
    except Exception:
        db_stats = {"total_trades": 0, "wins": 0, "losses": 0, "open_positions": 0,
                    "win_rate": 0, "total_pnl": 0, "total_volume": 0, "pnl_24h": 0, "trades_24h": 0}
    bot = request.app.state.bot
    live_stats = {}
    if bot and getattr(bot, "weather_detector", None):
        live_stats = bot.weather_detector.get_stats()
    feed_status = {}
    if bot and getattr(bot, "weather_feed", None):
        feed_status = bot.weather_feed.get_status()
    paper_stats = {}
    if bot and getattr(bot, "weather_paper", None):
        paper_stats = bot.weather_paper.get_stats()
    # Aplanar: live_stats tiene active_markets, signals_today etc.
    # paper_stats tiene paper trading stats
    merged = {**db_stats}
    merged["active_markets"] = live_stats.get("active_markets", 0)
    merged["signals_today"] = live_stats.get("signals_today", 0)
    merged["cities_monitored"] = live_stats.get("cities_monitored", 0)
    merged["live"] = live_stats
    merged["feed"] = feed_status
    merged["paper"] = paper_stats
    return merged


@router.get("/api/weather-arb/signals")
async def weather_arb_signals(request: Request, limit: int = 50):
    """Señales en vivo del detector weather."""
    bot = request.app.state.bot
    if bot and getattr(bot, "weather_detector", None):
        return {"signals": bot.weather_detector.get_recent_signals(limit)}
    return {"signals": []}


@router.get("/api/weather-arb/markets")
async def weather_arb_markets(request: Request):
    """Mercados weather activos con probabilidades ensemble vs odds."""
    bot = request.app.state.bot
    if bot and getattr(bot, "weather_detector", None):
        return {"markets": bot.weather_detector.get_active_markets()}
    return {"markets": []}


@router.get("/api/weather-arb/trades")
async def weather_arb_trades(request: Request, hours: int = 168, limit: int = 200):
    """Trades weather (reales) recientes."""
    db = request.app.state.db
    uid = await get_user_id(request)
    trades = await db.get_weather_trades(hours=hours, limit=limit, user_id=uid)
    return {"trades": trades}


@router.get("/api/weather-arb/paper-trades")
async def weather_arb_paper_trades(request: Request, limit: int = 200):
    """Paper trades del weather arb con PnL en tiempo real."""
    bot = request.app.state.bot
    if bot and getattr(bot, "weather_paper", None):
        # Actualizar precios en vivo antes de devolver
        try:
            await bot.weather_paper.update_live_prices()
        except Exception:
            pass
        return {
            "trades": bot.weather_paper.get_trades(limit),
            "stats": bot.weather_paper.get_stats(),
        }
    return {"trades": [], "stats": {}}


@router.get("/api/weather-arb/pnl-history")
async def weather_arb_pnl_history(request: Request, days: int = 30):
    """Historial diario de PnL weather."""
    db = request.app.state.db
    uid = await get_user_id(request)
    return await db.get_weather_pnl_history(days=days, user_id=uid)


@router.get("/api/weather-arb/autotrade-config")
async def get_weather_autotrade_config(request: Request):
    """Obtener configuración del weather autotrader (sin credenciales)."""
    db = request.app.state.db
    uid = await get_user_id(request)
    raw = await db.get_config_bulk([
        "wt_enabled", "wt_bet_size", "wt_min_edge", "wt_min_confidence",
        "wt_min_odds", "wt_max_odds", "wt_max_positions", "wt_order_type",
        "wt_max_daily_loss", "wt_max_daily_trades", "wt_cooldown_sec",
        "wt_cities",
        "wt_api_key", "wt_api_secret", "wt_private_key", "wt_passphrase",
        "wt_funder_address",
        "wt_bankroll", "wt_bet_mode", "wt_bet_pct",
        "wt_stop_loss_enabled", "wt_stop_loss_pct",
        "wt_take_profit_pct", "wt_max_holding_sec",
    ], user_id=uid)
    # Estado del motor
    bot = request.app.state.bot
    wt = getattr(bot, "weather_autotrader", None) if bot else None
    wt_status = wt.get_status() if wt else {}
    wallet_addr = _derive_wallet_address(raw.get("wt_private_key", ""))
    balance = None
    if wt and wallet_addr:
        try:
            balance = await wt.get_usdc_balance(wallet_addr)
        except Exception:
            pass
    stats = await db.get_weather_trade_stats(user_id=uid)
    return {
        "enabled": raw.get("wt_enabled") == "true",
        "bankroll": float(raw.get("wt_bankroll", 0)),
        "bet_mode": raw.get("wt_bet_mode", "fixed"),
        "bet_size": float(raw.get("wt_bet_size", 1)),
        "bet_pct": float(raw.get("wt_bet_pct", 2)),
        "min_edge": float(raw.get("wt_min_edge", 8)),
        "min_confidence": float(raw.get("wt_min_confidence", 50)),
        "min_odds": float(raw.get("wt_min_odds", 0.05)),
        "max_odds": float(raw.get("wt_max_odds", 0.85)),
        "max_positions": int(raw.get("wt_max_positions", 5)),
        "order_type": raw.get("wt_order_type", "market"),
        "max_daily_loss": float(raw.get("wt_max_daily_loss", 100)),
        "max_daily_trades": int(raw.get("wt_max_daily_trades", 20)),
        "cooldown_sec": int(raw.get("wt_cooldown_sec", 60)),
        "cities": [c.strip() for c in raw.get("wt_cities", "").split(",") if c.strip()] or [],
        "api_key_set": bool(raw.get("wt_api_key")),
        "api_secret_set": bool(raw.get("wt_api_secret")),
        "private_key_set": bool(raw.get("wt_private_key")),
        "passphrase_set": bool(raw.get("wt_passphrase")),
        "api_key_preview": (raw.get("wt_api_key", "")[:12] + "...") if raw.get("wt_api_key") else "",
        "wallet_address": wallet_addr,
        "connected": wt_status.get("connected", False),
        "balance": balance,
        "bankroll_available": wt_status.get("bankroll_available", 0),
        "bankroll_in_play": wt_status.get("bankroll_in_play", 0),
        "funder_address": raw.get("wt_funder_address", ""),
        "funder_address_set": bool(raw.get("wt_funder_address")),
        "open_positions": stats.get("open_positions", 0),
        "pnl_today": stats.get("pnl_24h", 0),
        "trades_today": stats.get("trades_24h", 0),
        "total_pnl": stats.get("total_pnl", 0),
        "win_rate": stats.get("win_rate", 0),
        "stop_loss_enabled": raw.get("wt_stop_loss_enabled") == "true",
        "stop_loss_pct": float(raw.get("wt_stop_loss_pct", 30)),
        "take_profit_pct": float(raw.get("wt_take_profit_pct", 50)),
        "max_holding_sec": int(raw.get("wt_max_holding_sec", 86400)),
    }


@router.post("/api/weather-arb/autotrade-config")
async def save_weather_autotrade_config(request: Request):
    """Guardar configuración del weather autotrader."""
    db = request.app.state.db
    uid = await get_user_id(request)
    body = await request.json()
    data = {}
    field_map = {
        "enabled": ("wt_enabled", lambda v: "true" if v else "false"),
        "bankroll": ("wt_bankroll", str),
        "bet_mode": ("wt_bet_mode", str),
        "bet_size": ("wt_bet_size", str),
        "bet_pct": ("wt_bet_pct", str),
        "min_edge": ("wt_min_edge", str),
        "min_confidence": ("wt_min_confidence", str),
        "min_odds": ("wt_min_odds", str),
        "max_odds": ("wt_max_odds", str),
        "max_positions": ("wt_max_positions", str),
        "order_type": ("wt_order_type", str),
        "max_daily_loss": ("wt_max_daily_loss", str),
        "max_daily_trades": ("wt_max_daily_trades", str),
        "cooldown_sec": ("wt_cooldown_sec", str),
        "api_key": ("wt_api_key", str),
        "api_secret": ("wt_api_secret", str),
        "private_key": ("wt_private_key", str),
        "passphrase": ("wt_passphrase", str),
        "funder_address": ("wt_funder_address", str),
        "stop_loss_enabled": ("wt_stop_loss_enabled", lambda v: "true" if v else "false"),
        "stop_loss_pct": ("wt_stop_loss_pct", str),
        "take_profit_pct": ("wt_take_profit_pct", str),
        "max_holding_sec": ("wt_max_holding_sec", str),
    }
    for key, (db_key, transform) in field_map.items():
        if key in body:
            data[db_key] = transform(body[key])
    if "cities" in body:
        data["wt_cities"] = ",".join(body["cities"]) if isinstance(body["cities"], list) else str(body["cities"])
    if data:
        await db.set_config_bulk(data, user_id=uid)
    # Recargar autotrader
    bot = request.app.state.bot
    if bot and getattr(bot, "weather_autotrader", None):
        await bot.weather_autotrader.reload_config(user_id=uid)
    return {"status": "ok", "saved": len(data)}


@router.post("/api/weather-arb/test-connection")
async def weather_test_connection(request: Request):
    """Probar conexión de la wallet weather."""
    bot = request.app.state.bot
    if not bot or not getattr(bot, "weather_autotrader", None):
        return {"connected": False, "error": "Weather autotrader no activo"}
    return await bot.weather_autotrader.test_connection()


@router.delete("/api/weather-arb/trades")
async def delete_weather_trades(request: Request):
    """Borrar todos los weather trades del usuario."""
    db = request.app.state.db
    uid = await get_user_id(request)
    try:
        async with db._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM weather_trades WHERE user_id = $1", uid)
            deleted = int(result.split(" ")[-1]) if result else 0
        bot = request.app.state.bot
        if bot and getattr(bot, "weather_autotrader", None):
            bot.weather_autotrader.reset_state()
        if bot and getattr(bot, "weather_paper", None):
            bot.weather_paper.clear()
        return {"status": "ok", "deleted": deleted}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.post("/api/weather-arb/generate-keys")
async def generate_weather_api_keys(request: Request):
    """Generar API Key, Secret y Passphrase para weather arb a partir de la Private Key.

    Solo necesita la private key — genera las otras 3 credenciales
    automáticamente via EIP-712 signing + CLOB API.
    """
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
        return {"status": "error", "error": f"Private key debe tener 64 caracteres hex (sin 0x). La tuya tiene {len(pk_clean)}."}

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
                        error_msg = f"CLOB respondió HTTP {resp.status_code}: {resp.text[:200]}"
                except Exception as e:
                    error_msg = str(e)

        if not api_creds:
            return {"status": "error", "error": error_msg or "No se pudieron generar las API keys."}

        print(f"[WeatherGenKeys] CLOB response keys: {list(api_creds.keys())}", flush=True)

        api_key = api_creds.get("apiKey", "")
        api_secret = api_creds.get("secret", "")
        passphrase = api_creds.get("passphrase", "")

        # Detectar proxy wallet (funder)
        funder_address = ""
        try:
            from py_clob_client.client import ClobClient as _ClobClient
            from py_clob_client.clob_types import ApiCreds as _ApiCreds
            _creds = _ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=passphrase)
            _temp_client = _ClobClient(
                "https://clob.polymarket.com",
                key=private_key, chain_id=137, signature_type=2, creds=_creds,
            )
            try:
                api_keys_resp = _temp_client.get_api_keys()
                print(f"[WeatherGenKeys] get_api_keys response: {api_keys_resp}", flush=True)
                if isinstance(api_keys_resp, list):
                    for k in api_keys_resp:
                        if isinstance(k, dict):
                            for field in ["proxyAddress", "funder", "proxy_address", "makerAddress", "maker"]:
                                if k.get(field):
                                    funder_address = k[field]
                                    break
                        if funder_address:
                            break
                elif isinstance(api_keys_resp, dict):
                    for field in ["proxyAddress", "funder", "proxy_address", "makerAddress", "maker"]:
                        if api_keys_resp.get(field):
                            funder_address = api_keys_resp[field]
                            break
            except Exception as e2:
                print(f"[WeatherGenKeys] get_api_keys falló: {e2}", flush=True)
        except Exception as e1:
            print(f"[WeatherGenKeys] Detección de proxy falló: {e1}", flush=True)

        if not funder_address:
            try:
                async with _httpx.AsyncClient(timeout=10) as gamma_client:
                    gamma_resp = await gamma_client.get(
                        f"https://gamma-api.polymarket.com/nonce",
                        params={"address": wallet_address}
                    )
                    if gamma_resp.status_code == 200:
                        gamma_data = gamma_resp.json()
                        for field in ["proxyAddress", "proxy_address", "polyAddress", "address"]:
                            val = gamma_data.get(field, "")
                            if val and val.lower() != wallet_address.lower():
                                funder_address = val
                                break
            except Exception:
                pass

        config_to_save = {
            "wt_private_key": pk_clean,
            "wt_api_key": api_key,
            "wt_api_secret": api_secret,
            "wt_passphrase": passphrase,
        }
        if funder_address:
            config_to_save["wt_funder_address"] = funder_address

        await db.set_config_bulk(config_to_save, user_id=uid)

        # Recargar config en weather autotrader
        bot = request.app.state.bot
        if bot and getattr(bot, "weather_autotrader", None):
            try:
                await bot.weather_autotrader.reload_config(user_id=uid)
            except Exception:
                pass

        msg = "API Keys generadas y guardadas para Weather Arb."
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
        return {"status": "error", "error": "Dependencia eth-account no instalada."}
    except ValueError as e:
        return {"status": "error", "error": f"Private key inválida: {e}"}
    except Exception as e:
        return {"status": "error", "error": f"Error generando keys: {e}"}


@router.post("/api/weather-arb/refresh-forecasts")
async def weather_refresh_forecasts(request: Request):
    """Forzar refresh de forecasts."""
    bot = request.app.state.bot
    if bot and getattr(bot, "weather_feed", None):
        try:
            await bot.weather_feed.refresh_now()
            return {"status": "ok", "message": "Forecasts actualizados"}
        except Exception as e:
            return {"status": "error", "error": str(e)}
    return {"status": "error", "error": "Weather feed no activo"}
