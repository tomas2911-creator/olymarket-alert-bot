"""Wallet AI Analyzer — Clasificación y scoring de wallets para copy trading.

Analiza los últimos trades de una wallet y determina:
- Tipo de trader (direccional, scalper, live bettor, bot, holder)
- Patrones de apuestas (fragmentación, ambos lados, hold time, etc.)
- Categorías favoritas (deportes, esports, política, crypto)
- Score de copiabilidad (1-10)
- Opinión AI textual con recomendación
"""

import asyncio
from collections import defaultdict
from datetime import datetime, timezone

import httpx

POLYMARKET_ACTIVITY_URL = "https://data-api.polymarket.com/activity"


def _parse_ts(val) -> float:
    """Convertir timestamp (ISO string o epoch) a unix float."""
    if not val:
        return 0
    if isinstance(val, (int, float)):
        return float(val)
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0


async def fetch_wallet_trades(address: str, limit: int = 50) -> list[dict]:
    """Fetch últimos trades de una wallet desde Polymarket API."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                POLYMARKET_ACTIVITY_URL,
                params={"user": address.lower(), "limit": limit},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[WalletAI] Error fetching trades for {address[:12]}: {e}", flush=True)
        return []


def _extract_categories(trades: list[dict]) -> dict[str, int]:
    """Extraer categorías de los trades basándose en el título y slug."""
    cats = defaultdict(int)
    for t in trades:
        title = (t.get("title", "") or "").lower()
        slug = (t.get("slug", "") or t.get("eventSlug", "") or "").lower()
        combined = title + " " + slug

        if any(k in combined for k in ["cs2", "counter-strike", "lol", "league", "dota", "valorant", "overwatch", "esport"]):
            cats["esports"] += 1
        elif any(k in combined for k in ["nba", "nfl", "mlb", "nhl", "soccer", "football", "tennis", "cricket",
                                          "t20", "odi", "premier league", "champions", "serie a", "la liga",
                                          "bundesliga", "ufc", "boxing", "f1", "formula", "golf", "rugby",
                                          "wrexham", "afc", "world cup", "atletico", "madrid", "brugge"]):
            cats["sports"] += 1
        elif any(k in combined for k in ["trump", "biden", "election", "congress", "senate", "president",
                                          "political", "vote", "governor", "republican", "democrat"]):
            cats["politics"] += 1
        elif any(k in combined for k in ["bitcoin", "btc", "eth", "ethereum", "crypto", "solana", "sol",
                                          "price", "market cap"]):
            cats["crypto"] += 1
        elif any(k in combined for k in ["weather", "temperature", "rain", "snow", "climate"]):
            cats["weather"] += 1
        else:
            cats["other"] += 1

    return dict(cats)


def _classify_trader_type(trades: list[dict]) -> str:
    """Clasificar el tipo de trader basándose en patrones de trading.

    Tipos:
    - DIRECTIONAL: compra un lado y mantiene hasta resolución
    - SCALPER: compra/vende rápido el mismo mercado por spread
    - LIVE_BETTOR: opera durante eventos en vivo, cambia de lado
    - BOT: alta frecuencia, tamaños uniformes
    - HOLDER: solo compras, nunca vende (espera resolución)
    """
    if not trades:
        return "unknown"

    # Agrupar trades por mercado (conditionId)
    markets = defaultdict(list)
    for t in trades:
        cid = t.get("conditionId", "")
        if cid:
            markets[cid].append(t)

    total_trades = len(trades)
    buys = [t for t in trades if t.get("side") == "BUY"]
    sells = [t for t in trades if t.get("side") == "SELL"]
    buy_count = len(buys)
    sell_count = len(sells)

    # Métricas por mercado
    markets_with_both_sides = 0  # Mercados donde opera ambos outcomes
    markets_with_buy_sell = 0    # Mercados donde compra Y vende
    rapid_turnaround_count = 0   # Mercados con buy→sell en < 30min
    fragment_count = 0           # Mercados con >3 trades del mismo lado

    for cid, mkt_trades in markets.items():
        sides = set(t.get("side") for t in mkt_trades)
        outcomes = set(t.get("outcome", "") for t in mkt_trades)

        if len(outcomes) > 1:
            markets_with_both_sides += 1

        if "BUY" in sides and "SELL" in sides:
            markets_with_buy_sell += 1
            # Verificar turnaround rápido
            buy_ts = [_parse_ts(t.get("timestamp")) for t in mkt_trades if t.get("side") == "BUY"]
            sell_ts = [_parse_ts(t.get("timestamp")) for t in mkt_trades if t.get("side") == "SELL"]
            if buy_ts and sell_ts:
                min_buy = min(buy_ts)
                min_sell = min(sell_ts)
                if abs(min_sell - min_buy) < 1800:  # 30 minutos
                    rapid_turnaround_count += 1

        # Fragmentación: muchos trades del mismo lado en el mismo mercado
        side_counts = defaultdict(int)
        for t in mkt_trades:
            side_counts[t.get("side", "")] += 1
        if any(c > 3 for c in side_counts.values()):
            fragment_count += 1

    total_markets = len(markets)
    if total_markets == 0:
        return "unknown"

    # Métricas de tamaño
    sizes = [float(t.get("usdcSize", 0)) for t in trades if float(t.get("usdcSize", 0)) > 0]
    if sizes:
        size_std = _std_dev(sizes)
        size_mean = sum(sizes) / len(sizes)
        size_cv = size_std / size_mean if size_mean > 0 else 0
    else:
        size_cv = 0

    # Timestamps: intervalo entre trades
    timestamps = sorted(_parse_ts(t.get("timestamp")) for t in trades if _parse_ts(t.get("timestamp")) > 0)
    if len(timestamps) > 1:
        intervals = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]
        avg_interval = sum(intervals) / len(intervals)
        trades_per_hour = 3600 / avg_interval if avg_interval > 0 else 0
    else:
        avg_interval = 0
        trades_per_hour = 0

    # ── Clasificación ──

    # BOT: alta frecuencia + tamaños uniformes
    if trades_per_hour > 10 and size_cv < 0.3 and total_trades > 20:
        return "bot"

    # SCALPER: compra/vende rápido el mismo mercado
    if rapid_turnaround_count > 0 and rapid_turnaround_count >= total_markets * 0.3:
        return "scalper"

    # LIVE_BETTOR: opera ambos lados del mismo mercado con cambios de lado
    if markets_with_both_sides > 0 and markets_with_both_sides >= total_markets * 0.3:
        return "live_bettor"

    # HOLDER: casi solo compras, no vende
    if sell_count == 0 or (sell_count / max(buy_count, 1)) < 0.1:
        return "holder"

    # DIRECTIONAL: compra un lado, vende al resolver (sell a 0.99+)
    high_price_sells = sum(1 for t in sells if float(t.get("price", 0)) >= 0.95)
    if high_price_sells >= sell_count * 0.5:
        return "directional"

    # Si compra y vende pero no es scalper ni live bettor
    if markets_with_buy_sell > 0 and markets_with_buy_sell < total_markets * 0.3:
        return "directional"

    return "mixed"


def _analyze_patterns(trades: list[dict]) -> dict:
    """Analizar patrones detallados de trading."""
    if not trades:
        return {}

    markets = defaultdict(list)
    for t in trades:
        cid = t.get("conditionId", "")
        if cid:
            markets[cid].append(t)

    total_trades = len(trades)
    total_markets = len(markets)
    buys = [t for t in trades if t.get("side") == "BUY"]
    sells = [t for t in trades if t.get("side") == "SELL"]

    # Tamaños
    sizes = [float(t.get("usdcSize", 0)) for t in trades if float(t.get("usdcSize", 0)) > 0]
    avg_size = sum(sizes) / len(sizes) if sizes else 0
    max_size = max(sizes) if sizes else 0
    total_volume = sum(sizes)

    # Precios de entrada (solo BUY)
    buy_prices = [float(t.get("price", 0)) for t in buys if 0 < float(t.get("price", 0)) < 1]
    avg_entry_price = sum(buy_prices) / len(buy_prices) if buy_prices else 0

    # Hold time: diferencia entre primer BUY y último SELL del mismo mercado
    hold_times = []
    for cid, mkt_trades in markets.items():
        mkt_buys = [t for t in mkt_trades if t.get("side") == "BUY"]
        mkt_sells = [t for t in mkt_trades if t.get("side") == "SELL"]
        if mkt_buys and mkt_sells:
            first_buy = min(_parse_ts(t.get("timestamp")) for t in mkt_buys)
            last_sell = max(_parse_ts(t.get("timestamp")) for t in mkt_sells)
            if last_sell > first_buy:
                hold_times.append(last_sell - first_buy)

    avg_hold_sec = sum(hold_times) / len(hold_times) if hold_times else 0

    # Resolución: ventas a precio >= 0.95 (indica hold hasta resolución)
    resolution_sells = sum(1 for t in sells if float(t.get("price", 0)) >= 0.95)
    sells_before_resolution = len(sells) - resolution_sells

    # Ambos lados
    markets_both_sides = 0
    for cid, mkt_trades in markets.items():
        outcomes = set(t.get("outcome", "") for t in mkt_trades if t.get("side") == "BUY")
        if len(outcomes) > 1:
            markets_both_sides += 1

    # Fragmentación
    max_trades_per_market = max(len(ts) for ts in markets.values()) if markets else 0

    # Formato hold time legible
    if avg_hold_sec > 86400:
        hold_label = f"{avg_hold_sec/86400:.1f} días"
    elif avg_hold_sec > 3600:
        hold_label = f"{avg_hold_sec/3600:.1f} horas"
    elif avg_hold_sec > 60:
        hold_label = f"{avg_hold_sec/60:.0f} min"
    else:
        hold_label = "< 1 min" if avg_hold_sec == 0 else f"{avg_hold_sec:.0f} seg"

    return {
        "total_trades": total_trades,
        "total_markets": total_markets,
        "unique_markets_ratio": round(total_markets / max(total_trades, 1), 2),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "avg_trade_size_usd": round(avg_size, 2),
        "max_trade_size_usd": round(max_size, 2),
        "total_volume_usd": round(total_volume, 2),
        "avg_entry_price": round(avg_entry_price, 3),
        "prefers_underdogs": avg_entry_price < 0.40 if buy_prices else False,
        "prefers_favorites": avg_entry_price > 0.65 if buy_prices else False,
        "avg_hold_time_sec": round(avg_hold_sec),
        "avg_hold_time_label": hold_label,
        "holds_to_resolution": resolution_sells > len(sells) * 0.5 if sells else True,
        "sells_before_resolution_pct": round(sells_before_resolution / max(len(sells), 1) * 100, 1),
        "trades_both_sides": markets_both_sides > 0,
        "markets_both_sides_count": markets_both_sides,
        "fragments_orders": max_trades_per_market > 5,
        "max_trades_per_market": max_trades_per_market,
    }


# Techo máximo de copiabilidad por tipo de trader
_TYPE_MAX_SCORE = {
    "directional": 10,
    "holder": 10,
    "mixed": 7,
    "live_bettor": 5,
    "scalper": 4,
    "bot": 3,
    "unknown": 6,
}


def _calculate_copiability(trader_type: str, patterns: dict, scan_data: dict) -> int:
    """Calcular score de copiabilidad 1-10."""
    score = 5  # Base

    # Tipo de trader — penalización fuerte para tipos no copiables
    type_scores = {
        "directional": 3,
        "holder": 3,
        "mixed": 1,
        "live_bettor": -3,
        "scalper": -4,
        "bot": -6,
        "unknown": 0,
    }
    score += type_scores.get(trader_type, 0)

    # Win rate del scan
    wr = scan_data.get("win_rate", 0)
    if wr >= 70:
        score += 2
    elif wr >= 60:
        score += 1

    # Profit factor
    pf = scan_data.get("profit_factor", 0)
    if pf >= 2.0:
        score += 1
    elif pf >= 1.5:
        score += 1

    # No opera ambos lados
    if not patterns.get("trades_both_sides", False):
        score += 1

    # Mantiene hasta resolución
    if patterns.get("holds_to_resolution", False):
        score += 1

    # No fragmenta demasiado
    if not patterns.get("fragments_orders", False):
        score += 1
    else:
        score -= 1

    # Mercados únicos (no repetir el mismo una y otra vez)
    umr = patterns.get("unique_markets_ratio", 0)
    if umr >= 0.6:
        score += 1
    elif umr < 0.3:
        score -= 1

    # Hold time muy bajo = scalping
    hold = patterns.get("avg_hold_time_sec", 0)
    if 0 < hold < 600:  # < 10 min
        score -= 1

    # Capital promedio muy bajo = no copiable
    avg_size = patterns.get("avg_trade_size_usd", 0)
    if 0 < avg_size < 5:
        score -= 2
    elif 0 < avg_size < 20:
        score -= 1

    # ROI positivo
    roi = scan_data.get("roi_pct", 0)
    if roi > 50:
        score += 1
    elif roi < -20:
        score -= 1

    # Aplicar techo máximo según tipo de trader
    max_score = _TYPE_MAX_SCORE.get(trader_type, 6)
    score = min(score, max_score)

    return max(1, min(10, score))


def _generate_opinion(trader_type: str, patterns: dict, copiability: int,
                      scan_data: dict) -> str:
    """Generar opinión AI textual sobre la wallet."""
    # Tipo
    type_labels = {
        "directional": "Trader direccional",
        "holder": "Holder (compra y espera resolución)",
        "scalper": "Scalper (compra/vende rápido por spread)",
        "live_bettor": "Apostador en vivo (opera durante eventos)",
        "bot": "Bot automatizado (alta frecuencia)",
        "mixed": "Trader mixto (sin patrón claro)",
        "unknown": "Tipo desconocido (pocos datos)",
    }
    tipo = type_labels.get(trader_type, "Trader")

    # Categoría principal
    cats = scan_data.get("_categories", {})
    if cats:
        main_cat = max(cats, key=cats.get)
        cat_label = {"esports": "esports", "sports": "deportes", "politics": "política",
                     "crypto": "crypto", "weather": "clima", "other": "varios"}.get(main_cat, main_cat)
    else:
        cat_label = "varios mercados"

    wr = scan_data.get("win_rate", 0)
    pf = scan_data.get("profit_factor", 0)
    roi = scan_data.get("roi_pct", 0)
    avg_size = patterns.get("avg_trade_size_usd", 0)
    hold_label = patterns.get("avg_hold_time_label", "desconocido")

    lines = [f"{tipo} especializado en {cat_label} con {wr:.0f}% win rate."]

    # Patrón principal
    if patterns.get("holds_to_resolution"):
        lines.append(f"Mantiene posiciones hasta resolución (hold promedio: {hold_label}).")
    elif patterns.get("avg_hold_time_sec", 0) > 0:
        lines.append(f"Hold promedio: {hold_label}.")

    if patterns.get("prefers_underdogs"):
        lines.append(f"Prefiere underdogs (precio entrada promedio: {patterns.get('avg_entry_price', 0):.2f}).")
    elif patterns.get("prefers_favorites"):
        lines.append(f"Prefiere favoritos (precio entrada promedio: {patterns.get('avg_entry_price', 0):.2f}).")

    if patterns.get("trades_both_sides"):
        n = patterns.get("markets_both_sides_count", 0)
        lines.append(f"⚠️ Opera ambos lados del mercado ({n} mercados).")

    if patterns.get("fragments_orders"):
        lines.append(f"Fragmenta órdenes ({patterns.get('max_trades_per_market', 0)} trades max por mercado).")

    lines.append(f"Capital promedio: ${avg_size:,.0f}/trade. PF: {pf:.2f}. ROI: {roi:.0f}%.")

    # Advertencias específicas por tipo no copiable
    if trader_type == "bot":
        lines.append("⛔ BOT: Alta frecuencia y timing preciso imposible de replicar en copy trading.")
    elif trader_type == "scalper":
        lines.append("⛔ SCALPER: Compra/vende rápido por spread, difícil de copiar con latencia.")
    elif trader_type == "live_bettor":
        lines.append("⚠️ LIVE BETTOR: Opera en vivo con cambios rápidos, complicado para copia pasiva.")

    if avg_size > 0 and avg_size < 5:
        lines.append(f"⚠️ Capital muy bajo (${avg_size:.1f}/trade), no viable para copiar.")

    # Recomendación — ajustada por tipo
    if trader_type in ("bot", "scalper"):
        lines.append("❌ NO RECOMENDADO para copy trading. Patrón incompatible con copia pasiva.")
        risk = "Muy Alto"
    elif copiability >= 8:
        lines.append("✅ EXCELENTE para copy trading. Trader disciplinado y rentable.")
        risk = "Bajo"
    elif copiability >= 6:
        lines.append("👍 BUENO para copy trading con precaución. Monitorear posiciones.")
        risk = "Medio"
    elif copiability >= 4:
        lines.append("⚠️ REGULAR para copy trading. Alto riesgo de trades no rentables al copiar.")
        risk = "Alto"
    else:
        lines.append("❌ NO RECOMENDADO para copy trading. Patrón incompatible con copia pasiva.")
        risk = "Muy Alto"

    lines.append(f"Riesgo: {risk}.")

    return " ".join(lines)


async def analyze_wallet(address: str, scan_data: dict, trades: list[dict] | None = None) -> dict:
    """Análisis completo de una wallet.

    Args:
        address: Dirección de la wallet
        scan_data: Datos del wallet_scan_cache (win_rate, pnl, etc.)
        trades: Trades precargados (opcional, si None se fetchean)

    Returns:
        dict con todos los campos para wallet_ai_analysis
    """
    if trades is None:
        trades = await fetch_wallet_trades(address)

    # Filtrar solo TRADE type
    trades = [t for t in trades if t.get("type") == "TRADE"]

    categories = _extract_categories(trades)
    trader_type = _classify_trader_type(trades)
    patterns = _analyze_patterns(trades)

    # Pasar categorías al generador de opinión
    scan_data_with_cats = dict(scan_data)
    scan_data_with_cats["_categories"] = categories

    copiability = _calculate_copiability(trader_type, patterns, scan_data_with_cats)
    opinion = _generate_opinion(trader_type, patterns, copiability, scan_data_with_cats)

    return {
        "address": address.lower(),
        "trader_type": trader_type,
        "copiability_score": copiability,
        "patterns": patterns,
        "categories": categories,
        "opinion": opinion,
        "win_rate": scan_data.get("win_rate", 0),
        "profit_factor": scan_data.get("profit_factor", 0),
        "avg_trade_size": patterns.get("avg_trade_size_usd", 0),
        "trades_analyzed": len(trades),
        "total_pnl": scan_data.get("total_pnl", 0),
        "roi_pct": scan_data.get("roi_pct", 0),
        "portfolio_value": scan_data.get("portfolio_value", 0),
    }


async def run_batch_analysis(db, addresses: list[str],
                             progress_callback=None) -> list[dict]:
    """Ejecutar análisis batch de múltiples wallets.

    Args:
        db: Database instance
        addresses: Lista de direcciones a analizar
        progress_callback: Función async callback(current, total, address) para progreso

    Returns:
        Lista de resultados de análisis
    """
    results = []
    total = len(addresses)

    for i, addr in enumerate(addresses):
        try:
            if progress_callback:
                await progress_callback(i, total, addr)

            # Obtener datos del scan cache
            scan_data = {}
            try:
                async with db._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT * FROM wallet_scan_cache WHERE LOWER(address) = $1",
                        addr.lower()
                    )
                    if row:
                        scan_data = dict(row)
            except Exception:
                pass

            # Analizar
            result = await analyze_wallet(addr, scan_data)
            results.append(result)

            # Guardar en DB
            await db.save_wallet_ai_analysis(result)

            print(f"[WalletAI] {i+1}/{total} — {addr[:12]}... → "
                  f"{result['trader_type']} (score={result['copiability_score']}/10)",
                  flush=True)

            # Rate limiting
            await asyncio.sleep(1.2)

        except Exception as e:
            print(f"[WalletAI] Error analyzing {addr[:12]}: {e}", flush=True)

    return results


def _std_dev(values: list[float]) -> float:
    """Desviación estándar simple."""
    if len(values) < 2:
        return 0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
    return variance ** 0.5
