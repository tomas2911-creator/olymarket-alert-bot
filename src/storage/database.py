"""PostgreSQL database for storing wallet stats, market baselines, alerts and resolution tracking."""
import asyncpg
from datetime import datetime, timedelta
from typing import Optional
import structlog

from src.models import WalletStats, MarketBaseline, Trade
from src import config

logger = structlog.get_logger()


class Database:
    """Async PostgreSQL database handler."""

    def __init__(self):
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        """Conectar a PostgreSQL y crear tablas."""
        dsn = config.DATABASE_URL
        if not dsn:
            raise RuntimeError("DATABASE_URL no configurada")
        self._pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
        await self._create_tables()
        logger.info("database_connected")

    async def close(self):
        if self._pool:
            await self._pool.close()
            logger.info("database_closed")

    # ── Schema ────────────────────────────────────────────────────────

    async def _create_tables(self):
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS wallets (
                    address        TEXT PRIMARY KEY,
                    total_trades   INTEGER DEFAULT 0,
                    first_seen     TIMESTAMPTZ,
                    last_seen      TIMESTAMPTZ,
                    total_volume   DOUBLE PRECISION DEFAULT 0,
                    avg_trade_size DOUBLE PRECISION DEFAULT 0,
                    markets_traded INTEGER DEFAULT 0,
                    win_count      INTEGER DEFAULT 0,
                    loss_count     INTEGER DEFAULT 0,
                    name           TEXT,
                    pseudonym      TEXT,
                    profile_image  TEXT
                );

                CREATE TABLE IF NOT EXISTS bot_config (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS trades (
                    transaction_hash TEXT PRIMARY KEY,
                    market_id        TEXT,
                    market_question  TEXT,
                    market_slug      TEXT,
                    wallet_address   TEXT,
                    side             TEXT,
                    outcome          TEXT,
                    size             DOUBLE PRECISION,
                    price            DOUBLE PRECISION,
                    timestamp        TIMESTAMPTZ,
                    market_end_date  TIMESTAMPTZ,
                    market_category  TEXT,
                    created_at       TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS market_baselines (
                    condition_id      TEXT PRIMARY KEY,
                    trade_count       INTEGER DEFAULT 0,
                    total_volume      DOUBLE PRECISION DEFAULT 0,
                    avg_trade_size    DOUBLE PRECISION DEFAULT 0,
                    median_trade_size DOUBLE PRECISION DEFAULT 0,
                    p90_trade_size    DOUBLE PRECISION DEFAULT 0,
                    p95_trade_size    DOUBLE PRECISION DEFAULT 0,
                    last_updated      TIMESTAMPTZ
                );

                CREATE TABLE IF NOT EXISTS alerts (
                    id              SERIAL PRIMARY KEY,
                    wallet_address  TEXT,
                    market_id       TEXT,
                    market_question TEXT,
                    market_slug     TEXT,
                    side            TEXT,
                    outcome         TEXT,
                    size            DOUBLE PRECISION,
                    price           DOUBLE PRECISION,
                    score           INTEGER,
                    triggers        TEXT,
                    cluster_wallets TEXT,
                    days_to_close   DOUBLE PRECISION,
                    wallet_hit_rate DOUBLE PRECISION,
                    resolved        BOOLEAN DEFAULT FALSE,
                    resolution      TEXT,
                    was_correct     BOOLEAN,
                    created_at      TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS markets_tracked (
                    condition_id TEXT PRIMARY KEY,
                    question     TEXT,
                    slug         TEXT,
                    end_date     TIMESTAMPTZ,
                    category     TEXT,
                    resolved     BOOLEAN DEFAULT FALSE,
                    resolution   TEXT,
                    updated_at   TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_trades_wallet    ON trades(wallet_address);
                CREATE INDEX IF NOT EXISTS idx_trades_market     ON trades(market_id);
                CREATE INDEX IF NOT EXISTS idx_trades_timestamp  ON trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_alerts_wallet     ON alerts(wallet_address, market_id);
                CREATE INDEX IF NOT EXISTS idx_alerts_created    ON alerts(created_at);
                CREATE INDEX IF NOT EXISTS idx_alerts_resolved   ON alerts(resolved);
                CREATE INDEX IF NOT EXISTS idx_markets_resolved  ON markets_tracked(resolved);
                CREATE INDEX IF NOT EXISTS idx_trades_category   ON trades(market_category);
            """)
            # Migraciones seguras para columnas nuevas
            migrations = [
                # Wallets: perfil
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS name TEXT",
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS pseudonym TEXT",
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS profile_image TEXT",
                # Wallets: PnL y smart money
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS total_pnl DOUBLE PRECISION DEFAULT 0",
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS total_cost DOUBLE PRECISION DEFAULT 0",
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS roi_pct DOUBLE PRECISION DEFAULT 0",
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS smart_money_score DOUBLE PRECISION DEFAULT 0",
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS correct_markets INTEGER DEFAULT 0",
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS avg_entry_price_correct DOUBLE PRECISION DEFAULT 0",
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS is_watchlisted BOOLEAN DEFAULT FALSE",
                # Wallets: on-chain
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS on_chain_first_tx TIMESTAMPTZ",
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS on_chain_funded_by TEXT",
                # Alerts: price impact y copy-trade
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS market_category TEXT",
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS price_at_alert DOUBLE PRECISION",
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS price_1h DOUBLE PRECISION",
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS price_6h DOUBLE PRECISION",
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS price_24h DOUBLE PRECISION",
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS is_copy_trade BOOLEAN DEFAULT FALSE",
                # Wallets: on-chain expandido
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS on_chain_age_days INTEGER",
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS on_chain_tx_count INTEGER",
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS on_chain_usdc_in DOUBLE PRECISION DEFAULT 0",
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS on_chain_usdc_out DOUBLE PRECISION DEFAULT 0",
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS on_chain_erc1155_transfers INTEGER DEFAULT 0",
                "ALTER TABLE wallets ADD COLUMN IF NOT EXISTS on_chain_checked_at TIMESTAMPTZ",
                # Trades: PnL calculado
                "ALTER TABLE trades ADD COLUMN IF NOT EXISTS pnl DOUBLE PRECISION",
                "ALTER TABLE trades ADD COLUMN IF NOT EXISTS pnl_calculated BOOLEAN DEFAULT FALSE",
            ]
            for m in migrations:
                try:
                    await conn.execute(m)
                except Exception:
                    pass

            # Tabla de coordinación
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS wallet_links (
                    id SERIAL PRIMARY KEY,
                    wallet_a TEXT NOT NULL,
                    wallet_b TEXT NOT NULL,
                    shared_markets INTEGER DEFAULT 0,
                    same_side_pct DOUBLE PRECISION DEFAULT 0,
                    avg_time_diff_sec DOUBLE PRECISION DEFAULT 0,
                    confidence DOUBLE PRECISION DEFAULT 0,
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(wallet_a, wallet_b)
                );
                CREATE INDEX IF NOT EXISTS idx_wallet_links_a ON wallet_links(wallet_a);
                CREATE INDEX IF NOT EXISTS idx_wallet_links_b ON wallet_links(wallet_b);
                CREATE INDEX IF NOT EXISTS idx_wallets_watchlist ON wallets(is_watchlisted);
                CREATE INDEX IF NOT EXISTS idx_wallets_smart ON wallets(smart_money_score DESC);
            """)

            # Tabla de señales crypto arb
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS crypto_signals (
                    id SERIAL PRIMARY KEY,
                    coin TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    spot_change_pct DOUBLE PRECISION,
                    poly_odds DOUBLE PRECISION,
                    fair_odds DOUBLE PRECISION,
                    confidence DOUBLE PRECISION,
                    edge_pct DOUBLE PRECISION,
                    condition_id TEXT,
                    market_question TEXT,
                    spot_price DOUBLE PRECISION,
                    time_remaining_sec INTEGER,
                    -- Paper trading
                    paper_bet_size DOUBLE PRECISION DEFAULT 0,
                    paper_result TEXT,
                    paper_pnl DOUBLE PRECISION,
                    resolved BOOLEAN DEFAULT FALSE,
                    resolution TEXT,
                    event_slug TEXT DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                -- Migración: agregar event_slug si no existe
                ALTER TABLE crypto_signals ADD COLUMN IF NOT EXISTS event_slug TEXT DEFAULT '';
                CREATE INDEX IF NOT EXISTS idx_crypto_signals_created
                    ON crypto_signals(created_at);
                CREATE INDEX IF NOT EXISTS idx_crypto_signals_coin
                    ON crypto_signals(coin);
                CREATE INDEX IF NOT EXISTS idx_crypto_signals_resolved
                    ON crypto_signals(resolved);
            """)

    # ── Wallet Stats ──────────────────────────────────────────────────

    async def get_wallet_stats(self, address: str) -> Optional[WalletStats]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM wallets WHERE address = $1", address.lower()
            )
            if row:
                return WalletStats(
                    address=row["address"],
                    total_trades=row["total_trades"],
                    first_seen=row["first_seen"] or datetime.now(),
                    last_seen=row["last_seen"] or datetime.now(),
                    total_volume=row["total_volume"] or 0,
                    avg_trade_size=row["avg_trade_size"] or 0,
                    markets_traded=row["markets_traded"] or 0,
                    win_count=row["win_count"] or 0,
                    loss_count=row["loss_count"] or 0,
                )
        return None

    async def update_wallet_stats(self, trade: Trade):
        addr = trade.wallet_address.lower()
        now = datetime.now()
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO wallets (address, total_trades, first_seen, last_seen,
                    total_volume, avg_trade_size, name, pseudonym, profile_image)
                VALUES ($1, 1, $2, $2, $3, $3, $4, $5, $6)
                ON CONFLICT (address) DO UPDATE SET
                    total_trades = wallets.total_trades + 1,
                    last_seen = $2,
                    total_volume = wallets.total_volume + $3,
                    avg_trade_size = (wallets.total_volume + $3) / (wallets.total_trades + 1),
                    name = COALESCE(EXCLUDED.name, wallets.name),
                    pseudonym = COALESCE(EXCLUDED.pseudonym, wallets.pseudonym),
                    profile_image = COALESCE(EXCLUDED.profile_image, wallets.profile_image)
            """, addr, now, trade.size,
                trade.trader_name, trade.trader_pseudonym, trade.trader_profile_image)

    async def update_wallet_markets_count(self, address: str):
        """Actualizar cantidad de mercados distintos."""
        addr = address.lower()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(DISTINCT market_id) as cnt FROM trades WHERE wallet_address = $1",
                addr,
            )
            if row:
                await conn.execute(
                    "UPDATE wallets SET markets_traded = $1 WHERE address = $2",
                    row["cnt"], addr,
                )

    # ── Trades ────────────────────────────────────────────────────────

    async def record_trade(self, trade: Trade) -> bool:
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute("""
                    INSERT INTO trades
                    (transaction_hash, market_id, market_question, market_slug,
                     wallet_address, side, outcome, size, price, timestamp,
                     market_end_date, market_category)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                    ON CONFLICT (transaction_hash) DO NOTHING
                """,
                    trade.transaction_hash, trade.market_id,
                    trade.market_question, trade.market_slug,
                    trade.wallet_address.lower(), trade.side,
                    trade.outcome, trade.size, trade.price,
                    trade.timestamp, trade.market_end_date,
                    trade.market_category,
                )
                return "INSERT" in result
        except Exception as e:
            logger.warning("failed_to_record_trade", error=str(e))
            return False

    async def is_trade_processed(self, transaction_hash: str) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM trades WHERE transaction_hash = $1",
                transaction_hash,
            )
            return row is not None

    # ── Market Baselines ──────────────────────────────────────────────

    async def get_market_baseline(self, condition_id: str) -> Optional[MarketBaseline]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM market_baselines WHERE condition_id = $1",
                condition_id,
            )
            if row:
                return MarketBaseline(
                    condition_id=row["condition_id"],
                    trade_count=row["trade_count"],
                    total_volume=row["total_volume"],
                    avg_trade_size=row["avg_trade_size"],
                    median_trade_size=row["median_trade_size"],
                    p90_trade_size=row["p90_trade_size"],
                    p95_trade_size=row["p95_trade_size"],
                    last_updated=row["last_updated"] or datetime.now(),
                )
        return None

    async def update_market_baseline(self, condition_id: str):
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT size FROM trades WHERE market_id = $1 ORDER BY size",
                condition_id,
            )
            if not rows:
                return
            sizes = sorted([r["size"] for r in rows])
            n = len(sizes)
            total = sum(sizes)
            avg = total / n
            median = sizes[n // 2]
            p90 = sizes[int(n * 0.90)] if n > 10 else sizes[-1]
            p95 = sizes[int(n * 0.95)] if n > 20 else sizes[-1]

            await conn.execute("""
                INSERT INTO market_baselines
                (condition_id, trade_count, total_volume, avg_trade_size,
                 median_trade_size, p90_trade_size, p95_trade_size, last_updated)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (condition_id) DO UPDATE SET
                    trade_count=EXCLUDED.trade_count, total_volume=EXCLUDED.total_volume,
                    avg_trade_size=EXCLUDED.avg_trade_size, median_trade_size=EXCLUDED.median_trade_size,
                    p90_trade_size=EXCLUDED.p90_trade_size, p95_trade_size=EXCLUDED.p95_trade_size,
                    last_updated=EXCLUDED.last_updated
            """, condition_id, n, total, avg, median, p90, p95, datetime.now())

    # ── Alerts ────────────────────────────────────────────────────────

    async def should_alert(self, wallet: str, market_id: str, cooldown_hours: int = 6) -> bool:
        cutoff = datetime.now() - timedelta(hours=cooldown_hours)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM alerts
                WHERE wallet_address = $1 AND market_id = $2 AND created_at > $3
            """, wallet.lower(), market_id, cutoff)
            return row is None

    async def record_alert(
        self, wallet: str, market_id: str, market_question: str,
        market_slug: str, side: str, outcome: str, size: float,
        price: float, score: int, triggers: list[str],
        cluster_wallets: list[str], days_to_close: Optional[float],
        wallet_hit_rate: Optional[float],
    ) -> int:
        """Registrar alerta y devolver ID."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO alerts
                (wallet_address, market_id, market_question, market_slug,
                 side, outcome, size, price, score, triggers,
                 cluster_wallets, days_to_close, wallet_hit_rate)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                RETURNING id
            """,
                wallet.lower(), market_id, market_question, market_slug,
                side, outcome, size, price, score,
                "||".join(triggers), ",".join(cluster_wallets),
                days_to_close, wallet_hit_rate,
            )
            return row["id"] if row else 0

    # ── Market Tracking & Resolution ──────────────────────────────────

    async def track_market(self, market_id: str, question: str, slug: str,
                           end_date: Optional[datetime], category: Optional[str]):
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO markets_tracked (condition_id, question, slug, end_date, category, updated_at)
                VALUES ($1,$2,$3,$4,$5,NOW())
                ON CONFLICT (condition_id) DO UPDATE SET
                    question=EXCLUDED.question, slug=EXCLUDED.slug,
                    end_date=EXCLUDED.end_date, category=EXCLUDED.category,
                    updated_at=NOW()
            """, market_id, question, slug, end_date, category)

    async def get_unresolved_markets(self) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM markets_tracked WHERE resolved = FALSE"
            )
            return [dict(r) for r in rows]

    async def resolve_market(self, condition_id: str, resolution: str):
        """Marcar mercado como resuelto, calcular PnL, actualizar alertas y wallets."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE markets_tracked SET resolved = TRUE, resolution = $1, updated_at = NOW()
                WHERE condition_id = $2
            """, resolution, condition_id)

            # Actualizar alertas de este mercado (BUY: outcome=resolution, SELL: outcome!=resolution)
            await conn.execute("""
                UPDATE alerts SET resolved = TRUE, resolution = $1,
                    was_correct = CASE
                        WHEN side = 'BUY' THEN (outcome = $1)
                        WHEN side = 'SELL' THEN (outcome != $1)
                        ELSE FALSE
                    END
                WHERE market_id = $2 AND resolved = FALSE
            """, resolution, condition_id)

            # Actualizar win/loss de wallets (solo alertas)
            alert_rows = await conn.fetch(
                "SELECT DISTINCT wallet_address, was_correct FROM alerts WHERE market_id = $1 AND resolved = TRUE",
                condition_id,
            )
            for r in alert_rows:
                if r["was_correct"] is True:
                    await conn.execute(
                        "UPDATE wallets SET win_count = win_count + 1 WHERE address = $1",
                        r["wallet_address"],
                    )
                elif r["was_correct"] is False:
                    await conn.execute(
                        "UPDATE wallets SET loss_count = loss_count + 1 WHERE address = $1",
                        r["wallet_address"],
                    )

            # Calcular PnL para TODOS los trades BUY de este mercado
            buy_trades = await conn.fetch("""
                SELECT transaction_hash, wallet_address, outcome, size, price
                FROM trades
                WHERE market_id = $1 AND side = 'BUY' AND pnl_calculated IS NOT TRUE
            """, condition_id)

            wallet_pnl_delta: dict[str, tuple[float, float]] = {}  # addr -> (pnl, cost)
            wallet_correct_set: dict[str, bool] = {}

            for t in buy_trades:
                is_correct = (t["outcome"] == resolution)
                cost = t["size"] * t["price"] if t["price"] > 0 else t["size"]
                pnl = t["size"] * (1 - t["price"]) if is_correct else -(t["size"] * t["price"])

                await conn.execute(
                    "UPDATE trades SET pnl = $1, pnl_calculated = TRUE WHERE transaction_hash = $2",
                    pnl, t["transaction_hash"],
                )

                addr = t["wallet_address"]
                prev_pnl, prev_cost = wallet_pnl_delta.get(addr, (0.0, 0.0))
                wallet_pnl_delta[addr] = (prev_pnl + pnl, prev_cost + cost)
                if is_correct:
                    wallet_correct_set[addr] = True

            # Actualizar PnL acumulado en wallets
            for addr, (pnl_sum, cost_sum) in wallet_pnl_delta.items():
                correct_inc = 1 if wallet_correct_set.get(addr) else 0
                await conn.execute("""
                    UPDATE wallets SET
                        total_pnl = COALESCE(total_pnl, 0) + $1,
                        total_cost = COALESCE(total_cost, 0) + $2,
                        roi_pct = CASE WHEN (COALESCE(total_cost, 0) + $2) > 0
                            THEN ((COALESCE(total_pnl, 0) + $1) / (COALESCE(total_cost, 0) + $2)) * 100
                            ELSE 0 END,
                        correct_markets = COALESCE(correct_markets, 0) + $3
                    WHERE address = $4
                """, pnl_sum, cost_sum, correct_inc, addr)

    # ── Clustering ────────────────────────────────────────────────────

    async def find_cluster_wallets(self, market_id: str, side: str,
                                   outcome: str, window_minutes: int = 30,
                                   min_size: float = 1000) -> list[str]:
        """Encontrar wallets que apostaron en el mismo lado en una ventana reciente."""
        cutoff = datetime.now() - timedelta(minutes=window_minutes)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT wallet_address FROM trades
                WHERE market_id = $1 AND side = $2 AND outcome = $3
                  AND timestamp > $4 AND size >= $5
                ORDER BY wallet_address
            """, market_id, side, outcome, cutoff, min_size)
            return [r["wallet_address"] for r in rows]

    # ── Accumulation Detection ─────────────────────────────────────

    async def get_accumulation_info(self, wallet: str, market_id: str,
                                     outcome: str, hours: int = 24) -> dict:
        """Detectar si una wallet está acumulando posición en un mercado."""
        addr = wallet.lower()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT COUNT(*) as count, COALESCE(SUM(size), 0) as total_size
                FROM trades
                WHERE wallet_address = $1 AND market_id = $2
                  AND outcome = $3 AND side = 'BUY'
                  AND timestamp > NOW() - ($4 || ' hours')::INTERVAL
            """, addr, market_id, outcome, str(hours))
            return {"count": row["count"] or 0, "total_size": float(row["total_size"] or 0)}

    # ── Smart Cluster Count ────────────────────────────────────────

    async def count_smart_wallets_same_side(self, market_id: str, side: str,
                                             outcome: str, exclude_wallet: str,
                                             hours: int = 24) -> int:
        """Contar wallets con buen win rate que apostaron al mismo lado recientemente."""
        async with self._pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT COUNT(DISTINCT t.wallet_address)
                FROM trades t
                JOIN wallets w ON w.address = t.wallet_address
                WHERE t.market_id = $1 AND t.side = $2 AND t.outcome = $3
                  AND t.wallet_address != $4
                  AND t.timestamp > NOW() - ($5 || ' hours')::INTERVAL
                  AND (w.win_count + w.loss_count) >= 3
                  AND w.win_count::float / NULLIF(w.win_count + w.loss_count, 0) >= $6
            """, market_id, side, outcome, exclude_wallet.lower(), str(hours), config.SMART_WALLET_MIN_WINRATE)
            return count or 0

    # ── Category Edge Analysis ─────────────────────────────────────

    async def get_category_edge(self) -> list[dict]:
        """Analizar win rate por categoría para detectar dónde el bot tiene más edge."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT t.market_category as category,
                       COUNT(DISTINCT a.id) as total_alerts,
                       COUNT(DISTINCT CASE WHEN a.was_correct THEN a.id END) as correct,
                       COUNT(DISTINCT CASE WHEN a.resolved THEN a.id END) as resolved,
                       AVG(a.score) as avg_score,
                       SUM(a.size) as total_size
                FROM alerts a
                LEFT JOIN trades t ON t.transaction_hash = (
                    SELECT transaction_hash FROM trades
                    WHERE market_id = a.market_id AND wallet_address = a.wallet_address
                    LIMIT 1
                )
                WHERE t.market_category IS NOT NULL
                GROUP BY t.market_category
                HAVING COUNT(DISTINCT a.id) >= 3
                ORDER BY
                    CASE WHEN COUNT(DISTINCT CASE WHEN a.resolved THEN a.id END) > 0
                         THEN COUNT(DISTINCT CASE WHEN a.was_correct THEN a.id END)::float /
                              COUNT(DISTINCT CASE WHEN a.resolved THEN a.id END)
                         ELSE 0 END DESC
            """)
            result = []
            for r in rows:
                resolved = r["resolved"] or 0
                correct = r["correct"] or 0
                result.append({
                    "category": r["category"],
                    "total_alerts": r["total_alerts"],
                    "correct": correct,
                    "resolved": resolved,
                    "win_rate": round(correct / resolved * 100, 1) if resolved > 0 else 0,
                    "avg_score": round(float(r["avg_score"] or 0), 1),
                    "total_size": float(r["total_size"] or 0),
                })
            return result

    # ── Dashboard Queries ─────────────────────────────────────────────

    async def get_dashboard_stats(self) -> dict:
        async with self._pool.acquire() as conn:
            total_alerts = await conn.fetchval("SELECT COUNT(*) FROM alerts")
            resolved_alerts = await conn.fetchval("SELECT COUNT(*) FROM alerts WHERE resolved = TRUE")
            correct_alerts = await conn.fetchval("SELECT COUNT(*) FROM alerts WHERE was_correct = TRUE")
            total_trades = await conn.fetchval("SELECT COUNT(*) FROM trades")
            unique_wallets = await conn.fetchval("SELECT COUNT(DISTINCT wallet_address) FROM alerts")
            alerts_24h = await conn.fetchval(
                "SELECT COUNT(*) FROM alerts WHERE created_at > NOW() - INTERVAL '24 hours'"
            )
            avg_score = await conn.fetchval("SELECT COALESCE(AVG(score), 0) FROM alerts")
            return {
                "total_alerts": total_alerts or 0,
                "resolved_alerts": resolved_alerts or 0,
                "correct_alerts": correct_alerts or 0,
                "hit_rate": round((correct_alerts / resolved_alerts * 100) if resolved_alerts else 0, 1),
                "total_trades_processed": total_trades or 0,
                "unique_wallets_flagged": unique_wallets or 0,
                "alerts_last_24h": alerts_24h or 0,
                "avg_score": round(float(avg_score), 1),
            }

    async def get_recent_alerts(self, limit: int = 50) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM alerts ORDER BY created_at DESC LIMIT $1
            """, limit)
            result = []
            for r in rows:
                d = dict(r)
                for k, v in d.items():
                    if isinstance(v, datetime):
                        d[k] = v.isoformat()
                result.append(d)
            return result

    async def get_top_wallets(self, limit: int = 20) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT w.address,
                       w.total_trades, w.total_volume, w.avg_trade_size,
                       w.win_count, w.loss_count, w.markets_traded,
                       COUNT(a.id) as alert_count,
                       MAX(a.score) as max_score
                FROM wallets w
                JOIN alerts a ON a.wallet_address = w.address
                GROUP BY w.address, w.total_trades, w.total_volume,
                         w.avg_trade_size, w.win_count, w.loss_count, w.markets_traded
                ORDER BY alert_count DESC, max_score DESC
                LIMIT $1
            """, limit)
            return [dict(r) for r in rows]

    async def get_alerts_by_day(self, days: int = 30) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT DATE(created_at) as day,
                       COUNT(*) as count,
                       AVG(score) as avg_score,
                       SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) as correct
                FROM alerts
                WHERE created_at > NOW() - ($1 || ' days')::INTERVAL
                GROUP BY DATE(created_at)
                ORDER BY day
            """, str(days))
            return [{"day": str(r["day"]), "count": r["count"],
                     "avg_score": round(float(r["avg_score"] or 0), 1),
                     "correct": r["correct"] or 0} for r in rows]

    async def get_score_distribution(self) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT score, COUNT(*) as count
                FROM alerts GROUP BY score ORDER BY score
            """)
            return [{"score": r["score"], "count": r["count"]} for r in rows]

    async def get_market_alerts(self, market_id: str) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM alerts WHERE market_id = $1 ORDER BY created_at DESC
            """, market_id)
            result = []
            for r in rows:
                d = dict(r)
                for k, v in d.items():
                    if isinstance(v, datetime):
                        d[k] = v.isoformat()
                result.append(d)
            return result

    # ── Config Persistente ─────────────────────────────────────────────

    async def get_config(self) -> dict:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM bot_config")
            return {r["key"]: r["value"] for r in rows}

    async def get_config_bulk(self, keys: list[str]) -> dict:
        """Obtener múltiples valores de config por lista de keys."""
        all_config = await self.get_config()
        return {k: all_config[k] for k in keys if k in all_config}

    async def set_config(self, key: str, value: str):
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bot_config (key, value) VALUES ($1, $2)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, key, value)

    async def set_config_bulk(self, data: dict):
        async with self._pool.acquire() as conn:
            for k, v in data.items():
                await conn.execute("""
                    INSERT INTO bot_config (key, value) VALUES ($1, $2)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """, k, str(v))

    # ── Wallet Detail ──────────────────────────────────────────────────

    async def get_wallet_detail(self, address: str) -> Optional[dict]:
        addr = address.lower()
        async with self._pool.acquire() as conn:
            wallet = await conn.fetchrow("SELECT * FROM wallets WHERE address = $1", addr)
            if not wallet:
                return None

            trades = await conn.fetch("""
                SELECT transaction_hash, market_id, market_question, market_slug,
                       side, outcome, size, price, timestamp, market_category
                FROM trades WHERE wallet_address = $1
                ORDER BY timestamp DESC LIMIT 100
            """, addr)

            alerts = await conn.fetch("""
                SELECT id, market_question, market_slug, side, outcome,
                       size, score, triggers, resolved, was_correct,
                       resolution, price_at_alert, is_copy_trade, created_at
                FROM alerts WHERE wallet_address = $1
                ORDER BY created_at DESC LIMIT 50
            """, addr)

            # Mercados únicos
            markets = await conn.fetch("""
                SELECT market_id, market_question, market_slug,
                       COUNT(*) as trade_count, SUM(size) as total_size
                FROM trades WHERE wallet_address = $1
                GROUP BY market_id, market_question, market_slug
                ORDER BY total_size DESC LIMIT 20
            """, addr)

            w = dict(wallet)
            for k, v in w.items():
                if isinstance(v, datetime):
                    w[k] = v.isoformat()

            return {
                "wallet": w,
                "trades": [_serialize_row(t) for t in trades],
                "alerts": [_serialize_row(a) for a in alerts],
                "markets": [dict(m) for m in markets],
            }

    # ── Markets List ───────────────────────────────────────────────────

    async def get_tracked_markets(self, limit: int = 50) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT m.condition_id, m.question, m.slug, m.end_date,
                       m.category, m.resolved, m.resolution,
                       COUNT(DISTINCT a.id) as alert_count,
                       COUNT(DISTINCT t.transaction_hash) as trade_count,
                       COALESCE(SUM(t.size), 0) as total_volume
                FROM markets_tracked m
                LEFT JOIN alerts a ON a.market_id = m.condition_id
                LEFT JOIN trades t ON t.market_id = m.condition_id
                GROUP BY m.condition_id, m.question, m.slug, m.end_date,
                         m.category, m.resolved, m.resolution
                ORDER BY alert_count DESC, total_volume DESC
                LIMIT $1
            """, limit)
            return [_serialize_row(r) for r in rows]

    # ── Category Distribution ──────────────────────────────────────────

    async def get_category_distribution(self) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT COALESCE(market_category, 'unknown') as category,
                       COUNT(*) as trade_count,
                       SUM(size) as total_volume,
                       COUNT(DISTINCT wallet_address) as unique_wallets
                FROM trades
                WHERE market_category IS NOT NULL
                GROUP BY market_category
                ORDER BY trade_count DESC
            """)
            return [dict(r) for r in rows]

    async def get_alert_category_distribution(self) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT COALESCE(t.market_category, 'unknown') as category,
                       COUNT(DISTINCT a.id) as alert_count,
                       AVG(a.score) as avg_score
                FROM alerts a
                LEFT JOIN trades t ON t.transaction_hash = (
                    SELECT transaction_hash FROM trades
                    WHERE market_id = a.market_id AND wallet_address = a.wallet_address
                    LIMIT 1
                )
                GROUP BY t.market_category
                ORDER BY alert_count DESC
            """)
            return [{"category": r["category"], "alert_count": r["alert_count"],
                     "avg_score": round(float(r["avg_score"] or 0), 1)} for r in rows]

    # ── Recent Trades Feed ─────────────────────────────────────────────

    async def get_recent_trades_feed(self, limit: int = 100) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT t.transaction_hash, t.market_id, t.market_question,
                       t.market_slug, t.wallet_address, t.side, t.outcome,
                       t.size, t.price, t.timestamp, t.market_category,
                       w.total_trades as wallet_trades, w.name as wallet_name,
                       w.pseudonym as wallet_pseudonym
                FROM trades t
                LEFT JOIN wallets w ON w.address = t.wallet_address
                ORDER BY t.created_at DESC
                LIMIT $1
            """, limit)
            return [_serialize_row(r) for r in rows]


    # ── Smart Money Score ───────────────────────────────────────────────

    async def update_smart_money_score(self, address: str):
        """Calcular y guardar smart money score (0-100) para una wallet."""
        addr = address.lower()
        async with self._pool.acquire() as conn:
            w = await conn.fetchrow("SELECT * FROM wallets WHERE address = $1", addr)
            if not w:
                return
            wins = w["win_count"] or 0
            losses = w["loss_count"] or 0
            resolved = wins + losses
            if resolved < 3:
                return  # No hay suficientes datos

            total_pnl = float(w["total_pnl"] or 0)
            total_cost = float(w["total_cost"] or 0)
            correct_mkts = w["correct_markets"] or 0

            # 1. Win rate (0-30 pts)
            wr = wins / resolved
            pts_wr = wr * 30

            # 2. ROI (0-30 pts, cap at 200%)
            roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
            pts_roi = min(max(roi, 0) / 200, 1.0) * 30

            # 3. Consistency (0-20 pts, 10+ correct markets = max)
            pts_con = min(correct_mkts / 10, 1.0) * 20

            # 4. Early mover (0-20 pts, lower avg entry price = better)
            avg_price = await conn.fetchval("""
                SELECT AVG(price) FROM trades
                WHERE wallet_address = $1 AND pnl > 0 AND price > 0 AND price < 1
            """, addr)
            avg_price = float(avg_price) if avg_price else 0.5
            pts_early = max(0, 1 - avg_price) * 20

            score = round(pts_wr + pts_roi + pts_con + pts_early, 1)
            await conn.execute("""
                UPDATE wallets SET smart_money_score = $1,
                    avg_entry_price_correct = $2 WHERE address = $3
            """, score, avg_price, addr)

    async def update_all_smart_money_scores(self):
        """Recalcular smart money score para wallets con resoluciones."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT address FROM wallets
                WHERE (win_count + loss_count) >= 3
            """)
        count = 0
        for r in rows:
            try:
                await self.update_smart_money_score(r["address"])
                count += 1
            except Exception:
                pass
        return count

    # ── Watchlist ─────────────────────────────────────────────────────

    async def update_watchlist(self, threshold: float = 50, min_resolved: int = 3):
        """Auto-actualizar watchlist basado en smart money score."""
        async with self._pool.acquire() as conn:
            # Quitar todos del watchlist
            await conn.execute("UPDATE wallets SET is_watchlisted = FALSE WHERE is_watchlisted = TRUE")
            # Agregar los que califican
            result = await conn.execute("""
                UPDATE wallets SET is_watchlisted = TRUE
                WHERE smart_money_score >= $1
                  AND (win_count + loss_count) >= $2
            """, threshold, min_resolved)
            return result

    async def get_watchlisted_wallets(self) -> set[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT address FROM wallets WHERE is_watchlisted = TRUE"
            )
            return {r["address"] for r in rows}

    async def is_wallet_watchlisted(self, address: str) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT is_watchlisted FROM wallets WHERE address = $1",
                address.lower(),
            )
            return bool(row and row["is_watchlisted"])

    # ── Price Impact ─────────────────────────────────────────────────

    async def get_alerts_needing_price_check(self, field: str, min_hours: float, max_hours: float) -> list[dict]:
        """Obtener alertas que necesitan check de precio en un intervalo."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(f"""
                SELECT id, market_id, outcome, side, price_at_alert
                FROM alerts
                WHERE {field} IS NULL
                  AND created_at < NOW() - ($1 || ' hours')::INTERVAL
                  AND created_at > NOW() - ($2 || ' hours')::INTERVAL
                  AND resolved = FALSE
                ORDER BY created_at DESC
                LIMIT 20
            """, str(min_hours), str(max_hours))
            return [dict(r) for r in rows]

    async def update_alert_price_impact(self, alert_id: int, field: str, price: float):
        """Actualizar precio de seguimiento para una alerta."""
        if field not in ("price_1h", "price_6h", "price_24h"):
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"UPDATE alerts SET {field} = $1 WHERE id = $2",
                price, alert_id,
            )

    async def record_alert_with_price(self, **kwargs) -> int:
        """Record alert incluyendo price_at_alert y is_copy_trade."""
        price_at_alert = kwargs.pop("price_at_alert", None)
        is_copy_trade = kwargs.pop("is_copy_trade", False)
        alert_id = await self.record_alert(**kwargs)
        if alert_id and (price_at_alert is not None or is_copy_trade):
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    UPDATE alerts SET price_at_alert = $1, is_copy_trade = $2 WHERE id = $3
                """, price_at_alert, is_copy_trade, alert_id)
        return alert_id

    # ── Coordination Detection ────────────────────────────────────────

    async def detect_coordination(self):
        """Detectar pares de wallets que tradean coordinadamente (excluyendo crypto/sports)."""
        async with self._pool.acquire() as conn:
            # Limpiar links viejos antes de recalcular
            await conn.execute("DELETE FROM wallet_links WHERE updated_at < NOW() - INTERVAL '7 days'")

            # Encontrar pares que comparten 3+ mercados en el mismo lado
            # EXCLUIR categorías de ruido: sports, crypto-prices, updown
            pairs = await conn.fetch("""
                SELECT t1.wallet_address as wa, t2.wallet_address as wb,
                       COUNT(DISTINCT t1.market_id) as shared,
                       AVG(ABS(EXTRACT(EPOCH FROM (t1.timestamp - t2.timestamp)))) as avg_diff
                FROM trades t1
                JOIN trades t2 ON t1.market_id = t2.market_id
                    AND t1.side = t2.side
                    AND t1.outcome = t2.outcome
                    AND t1.wallet_address < t2.wallet_address
                    AND ABS(EXTRACT(EPOCH FROM (t1.timestamp - t2.timestamp))) < 300
                WHERE t1.timestamp > NOW() - INTERVAL '7 days'
                  AND t1.size >= 100
                  AND t2.size >= 100
                  AND COALESCE(LOWER(t1.market_category), '') NOT IN
                      ('sports','nba','nfl','nhl','mlb','mls','soccer','esports',
                       'crypto-prices','crypto-price','updown')
                GROUP BY t1.wallet_address, t2.wallet_address
                HAVING COUNT(DISTINCT t1.market_id) >= 3
                ORDER BY shared DESC
                LIMIT 50
            """)

            count = 0
            for p in pairs:
                # Calcular same_side percentage
                total_shared = await conn.fetchval("""
                    SELECT COUNT(DISTINCT t1.market_id)
                    FROM trades t1 JOIN trades t2
                        ON t1.market_id = t2.market_id
                        AND t1.wallet_address = $1 AND t2.wallet_address = $2
                    WHERE t1.size >= 100 AND t2.size >= 100
                """, p["wa"], p["wb"])
                same_pct = (p["shared"] / total_shared * 100) if total_shared else 0

                # Confianza más gradual: shared markets (0-40) + same_side% (0-30) + timing (0-30)
                shared_score = min(40, p["shared"] * 8)  # 5 markets = 40 pts max
                side_score = max(0, (same_pct - 50) / 50 * 30)  # >50% same side = 0-30 pts
                avg_diff = float(p["avg_diff"] or 300)
                timing_score = max(0, (300 - avg_diff) / 300 * 30)  # <5min = 0-30 pts
                confidence = min(100, shared_score + side_score + timing_score)

                await conn.execute("""
                    INSERT INTO wallet_links (wallet_a, wallet_b, shared_markets,
                        same_side_pct, avg_time_diff_sec, confidence, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, NOW())
                    ON CONFLICT (wallet_a, wallet_b) DO UPDATE SET
                        shared_markets = EXCLUDED.shared_markets,
                        same_side_pct = EXCLUDED.same_side_pct,
                        avg_time_diff_sec = EXCLUDED.avg_time_diff_sec,
                        confidence = EXCLUDED.confidence,
                        updated_at = NOW()
                """, p["wa"], p["wb"], p["shared"], same_pct,
                    avg_diff, confidence)
                count += 1
            return count

    async def get_wallet_coordination(self, address: str) -> list[dict]:
        addr = address.lower()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM wallet_links
                WHERE wallet_a = $1 OR wallet_b = $1
                ORDER BY confidence DESC LIMIT 10
            """, addr)
            return [_serialize_row(r) for r in rows]

    async def get_all_coordination(self, limit: int = 30) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT wl.*,
                    w1.pseudonym as name_a, w1.smart_money_score as score_a,
                    w2.pseudonym as name_b, w2.smart_money_score as score_b
                FROM wallet_links wl
                LEFT JOIN wallets w1 ON w1.address = wl.wallet_a
                LEFT JOIN wallets w2 ON w2.address = wl.wallet_b
                ORDER BY wl.confidence DESC
                LIMIT $1
            """, limit)
            return [_serialize_row(r) for r in rows]

    # ── Leaderboard ──────────────────────────────────────────────────

    async def get_leaderboard(self, limit: int = 30, sort_by: str = "pnl") -> list[dict]:
        order = {
            "pnl": "total_pnl DESC",
            "roi": "roi_pct DESC",
            "score": "smart_money_score DESC",
            "winrate": "(win_count::float / NULLIF(win_count + loss_count, 0)) DESC",
        }.get(sort_by, "total_pnl DESC")

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(f"""
                SELECT address, name, pseudonym, profile_image,
                       total_trades, total_volume, avg_trade_size,
                       win_count, loss_count, markets_traded,
                       total_pnl, total_cost, roi_pct,
                       smart_money_score, correct_markets,
                       is_watchlisted,
                       on_chain_first_tx, on_chain_funded_by,
                       on_chain_age_days, on_chain_tx_count,
                       on_chain_usdc_in, on_chain_usdc_out,
                       on_chain_erc1155_transfers
                FROM wallets
                WHERE (win_count + loss_count) > 0
                ORDER BY {order}
                LIMIT $1
            """, limit)
            return [_serialize_row(r) for r in rows]

    # ── Polygonscan Data ─────────────────────────────────────────────

    async def get_wallets_for_onchain_check(self, limit: int = 20) -> list[str]:
        """Wallets que nunca se chequearon o hace más de 24h."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT address FROM wallets
                WHERE (on_chain_checked_at IS NULL
                       OR on_chain_checked_at < NOW() - INTERVAL '24 hours')
                  AND total_trades >= 1
                ORDER BY
                    on_chain_checked_at IS NULL DESC,
                    total_volume DESC
                LIMIT $1
            """, limit)
            return [r["address"] for r in rows]

    async def update_wallet_onchain(self, address: str, data: dict):
        """Guardar datos on-chain expandidos de una wallet."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE wallets SET
                    on_chain_first_tx = COALESCE($1, on_chain_first_tx),
                    on_chain_funded_by = COALESCE($2, on_chain_funded_by),
                    on_chain_age_days = COALESCE($3, on_chain_age_days),
                    on_chain_tx_count = COALESCE($4, on_chain_tx_count),
                    on_chain_usdc_in = COALESCE($5, on_chain_usdc_in),
                    on_chain_usdc_out = COALESCE($6, on_chain_usdc_out),
                    on_chain_erc1155_transfers = COALESCE($7, on_chain_erc1155_transfers),
                    on_chain_checked_at = NOW()
                WHERE address = $8
            """,
                data.get("first_tx"),
                data.get("funded_by"),
                data.get("age_days"),
                data.get("tx_count"),
                data.get("usdc_in"),
                data.get("usdc_out"),
                data.get("erc1155_transfers"),
                address.lower(),
            )

    # ── Wallet Baskets ───────────────────────────────────────────────

    async def get_wallet_category_profile(self, address: str) -> dict:
        """Obtener el perfil de categorías de una wallet (en qué categorías suele operar)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT LOWER(m.category) as category, COUNT(*) as trade_count, SUM(t.size) as total_vol
                FROM trades t
                JOIN markets m ON m.condition_id = t.market_id
                WHERE t.wallet_address = $1 AND m.category IS NOT NULL
                GROUP BY LOWER(m.category)
                ORDER BY trade_count DESC
            """, address.lower())
            total = sum(r["trade_count"] for r in rows) or 1
            return {
                "categories": {
                    r["category"]: {
                        "count": r["trade_count"],
                        "volume": float(r["total_vol"] or 0),
                        "pct": round(r["trade_count"] / total * 100, 1),
                    }
                    for r in rows
                },
                "primary_category": rows[0]["category"] if rows else None,
                "total_trades": total,
            }

    async def find_basket_wallets_in_market(self, market_id: str, market_category: str,
                                             window_hours: int = 24) -> list[dict]:
        """Encontrar wallets que normalmente NO operan en esta categoría pero apostaron aquí."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                WITH wallet_trades AS (
                    SELECT t.wallet_address, COUNT(*) as total_trades,
                           COUNT(CASE WHEN LOWER(m.category) = LOWER($2) THEN 1 END) as cat_trades
                    FROM trades t
                    JOIN markets m ON m.condition_id = t.market_id
                    WHERE t.wallet_address IN (
                        SELECT DISTINCT wallet_address FROM trades
                        WHERE market_id = $1
                          AND timestamp > NOW() - ($3 || ' hours')::INTERVAL
                    )
                    AND m.category IS NOT NULL
                    GROUP BY t.wallet_address
                    HAVING COUNT(*) >= $4
                )
                SELECT wallet_address, total_trades, cat_trades,
                       ROUND(cat_trades::numeric / NULLIF(total_trades, 0) * 100, 1) as cat_pct
                FROM wallet_trades
                WHERE cat_trades::float / NULLIF(total_trades, 0) < $5
                ORDER BY total_trades DESC
                LIMIT 20
            """, market_id, market_category.lower() if market_category else "", str(window_hours),
                config.BASKET_MIN_WALLET_TRADES, config.BASKET_CATEGORY_SHIFT_THRESHOLD)
            return [dict(r) for r in rows]

    # ── Sniper DBSCAN ────────────────────────────────────────────────

    async def get_trades_for_sniper_scan(self, market_id: str, side: str,
                                          outcome: str, window_minutes: int = 10,
                                          min_size: float = 500) -> list[dict]:
        """Obtener trades recientes de un mercado para análisis DBSCAN."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT wallet_address, size, timestamp,
                       EXTRACT(EPOCH FROM timestamp) as ts_epoch
                FROM trades
                WHERE market_id = $1 AND side = $2 AND outcome = $3
                  AND timestamp > NOW() - ($4 || ' minutes')::INTERVAL
                  AND size >= $5
                ORDER BY timestamp ASC
            """, market_id, side, outcome, str(window_minutes), min_size)
            return [dict(r) for r in rows]

    # ── Crypto Arb Signals ────────────────────────────────────────────

    async def record_crypto_signal(self, signal: dict) -> int:
        """Registrar señal crypto arb y devolver ID."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO crypto_signals
                (coin, direction, spot_change_pct, poly_odds, fair_odds,
                 confidence, edge_pct, condition_id, market_question,
                 spot_price, time_remaining_sec, paper_bet_size, event_slug)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                RETURNING id
            """,
                signal["coin"], signal["direction"],
                signal.get("spot_change_pct"), signal.get("poly_odds"),
                signal.get("fair_odds"), signal.get("confidence"),
                signal.get("edge_pct"), signal.get("condition_id"),
                signal.get("market_question"), signal.get("spot_price"),
                signal.get("time_remaining_sec"),
                signal.get("paper_bet_size", 0),
                signal.get("event_slug", ""),
            )
            return row["id"] if row else 0

    async def resolve_crypto_signal(self, signal_id: int, resolution: str,
                                     paper_result: str, paper_pnl: float):
        """Resolver señal crypto con resultado real."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE crypto_signals SET
                    resolved = TRUE, resolution = $1,
                    paper_result = $2, paper_pnl = $3
                WHERE id = $4
            """, resolution, paper_result, paper_pnl, signal_id)

    async def get_unresolved_crypto_signals(self) -> list[dict]:
        """Señales crypto pendientes de resolución."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM crypto_signals
                WHERE resolved = FALSE
                  AND created_at > NOW() - INTERVAL '2 hours'
                ORDER BY created_at DESC
            """)
            return [_serialize_row(r) for r in rows]

    async def get_crypto_signals_history(self, limit: int = 200,
                                          coin: str = None) -> list[dict]:
        """Historial de señales crypto para dashboard."""
        async with self._pool.acquire() as conn:
            if coin:
                rows = await conn.fetch("""
                    SELECT * FROM crypto_signals
                    WHERE coin = $1
                    ORDER BY created_at DESC LIMIT $2
                """, coin, limit)
            else:
                rows = await conn.fetch("""
                    SELECT * FROM crypto_signals
                    ORDER BY created_at DESC LIMIT $1
                """, limit)
            return [_serialize_row(r) for r in rows]

    async def get_crypto_arb_stats(self) -> dict:
        """Estadísticas del bot crypto arb."""
        async with self._pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM crypto_signals")
            resolved = await conn.fetchval(
                "SELECT COUNT(*) FROM crypto_signals WHERE resolved = TRUE")
            wins = await conn.fetchval(
                "SELECT COUNT(*) FROM crypto_signals WHERE paper_result = 'win'")
            total_pnl = await conn.fetchval(
                "SELECT COALESCE(SUM(paper_pnl), 0) FROM crypto_signals WHERE resolved = TRUE")
            today_count = await conn.fetchval(
                "SELECT COUNT(*) FROM crypto_signals WHERE created_at > NOW() - INTERVAL '24 hours'")
            today_pnl = await conn.fetchval(
                "SELECT COALESCE(SUM(paper_pnl), 0) FROM crypto_signals "
                "WHERE resolved = TRUE AND created_at > NOW() - INTERVAL '24 hours'")

            # Por moneda
            by_coin = await conn.fetch("""
                SELECT coin,
                       COUNT(*) as total,
                       COUNT(CASE WHEN paper_result = 'win' THEN 1 END) as wins,
                       COALESCE(SUM(paper_pnl), 0) as pnl
                FROM crypto_signals
                WHERE resolved = TRUE
                GROUP BY coin
            """)

            return {
                "total_signals": total or 0,
                "resolved": resolved or 0,
                "wins": wins or 0,
                "win_rate": round((wins / resolved * 100) if resolved else 0, 1),
                "total_pnl": round(float(total_pnl or 0), 2),
                "signals_24h": today_count or 0,
                "pnl_24h": round(float(today_pnl or 0), 2),
                "by_coin": [
                    {
                        "coin": r["coin"],
                        "total": r["total"],
                        "wins": r["wins"],
                        "pnl": round(float(r["pnl"]), 2),
                        "win_rate": round((r["wins"] / r["total"] * 100) if r["total"] else 0, 1),
                    }
                    for r in by_coin
                ],
            }


def _serialize_row(row) -> dict:
    """Convertir asyncpg Record a dict serializando datetimes."""
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d
