"""PostgreSQL database for storing wallet stats, market baselines, alerts and resolution tracking."""
import json
import asyncpg
from datetime import datetime, timedelta, timezone
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
        await self._ensure_admin_user()
        await self._cleanup_expired_sessions()
        logger.info("database_connected")

    async def close(self):
        if self._pool:
            await self._pool.close()
            logger.info("database_closed")

    async def _cleanup_expired_sessions(self):
        """Limpiar sesiones expiradas de la base de datos."""
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute("DELETE FROM user_sessions WHERE expires_at < NOW()")
                deleted = int(result.split(" ")[-1]) if result else 0
                if deleted:
                    logger.info(f"expired_sessions_cleaned count={deleted}")
        except Exception:
            pass  # No es crítico si falla

    async def _ensure_admin_user(self):
        """Garantizar que existe el usuario admin (id=1) con credenciales correctas."""
        import hashlib, secrets, os
        admin_user = os.getenv("ADMIN_USERNAME", "admin")
        admin_pass = os.getenv("ADMIN_PASSWORD", "admin1234")
        async with self._pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT id, username FROM users WHERE id = 1")
            if existing and existing["username"] == admin_user:
                # Admin ya existe con el username correcto — no tocar hash
                logger.info(f"admin_user_exists username={admin_user}")
                return
            if existing:
                # User id=1 existe pero con otro username — actualizar
                salt = secrets.token_hex(16)
                pw_hash = hashlib.sha256((salt + admin_pass).encode()).hexdigest() + ":" + salt
                await conn.execute("""
                    UPDATE users SET username = $1, password_hash = $2, display_name = $3
                    WHERE id = 1
                """, admin_user, pw_hash, "Admin")
                logger.info(f"admin_user_updated username={admin_user}")
            else:
                # Crear usuario admin con id=1
                salt = secrets.token_hex(16)
                pw_hash = hashlib.sha256((salt + admin_pass).encode()).hexdigest() + ":" + salt
                await conn.execute("""
                    INSERT INTO users (id, username, password_hash, display_name)
                    VALUES (1, $1, $2, $3)
                    ON CONFLICT (id) DO UPDATE SET username = $1, password_hash = $2, display_name = $3
                """, admin_user, pw_hash, "Admin")
                await conn.execute("SELECT setval('users_id_seq', GREATEST((SELECT MAX(id) FROM users), 1))")
                logger.info("admin_user_created id=1")

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
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS price_latest DOUBLE PRECISION",
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS price_latest_at TIMESTAMPTZ",
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
                # Alerts: resolved_at para backtest
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ",
                # Paper Trading PnL
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS paper_pnl DOUBLE PRECISION",
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS paper_shares DOUBLE PRECISION",
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS exit_price DOUBLE PRECISION",
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS exit_type TEXT",  # 'resolution' o 'wallet_exit'
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS exit_at TIMESTAMPTZ",
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
                -- Migración: agregar score_details JSONB para guardar score + indicadores
                ALTER TABLE crypto_signals ADD COLUMN IF NOT EXISTS score_details JSONB DEFAULT '{}'::jsonb;
                -- Migración: agregar strategy para diferenciar score vs early_entry
                ALTER TABLE crypto_signals ADD COLUMN IF NOT EXISTS strategy TEXT DEFAULT 'score';
                CREATE INDEX IF NOT EXISTS idx_crypto_signals_created
                    ON crypto_signals(created_at);
                CREATE INDEX IF NOT EXISTS idx_crypto_signals_coin
                    ON crypto_signals(coin);
                CREATE INDEX IF NOT EXISTS idx_crypto_signals_resolved
                    ON crypto_signals(resolved);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_crypto_signals_condition_id
                    ON crypto_signals(condition_id);
            """)

            # Tabla de autotrades (trades reales ejecutados)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS autotrades (
                    id              SERIAL PRIMARY KEY,
                    condition_id    TEXT NOT NULL,
                    order_id        TEXT DEFAULT '',
                    coin            TEXT NOT NULL,
                    direction       TEXT NOT NULL,
                    side            TEXT DEFAULT 'BUY',
                    price           DOUBLE PRECISION,
                    size_usd        DOUBLE PRECISION,
                    shares          DOUBLE PRECISION,
                    token_id        TEXT,
                    edge_pct        DOUBLE PRECISION,
                    confidence      DOUBLE PRECISION,
                    event_slug      TEXT DEFAULT '',
                    order_type      TEXT DEFAULT 'limit',
                    status          TEXT DEFAULT 'filled',
                    error           TEXT,
                    resolved        BOOLEAN DEFAULT FALSE,
                    result          TEXT,
                    pnl             DOUBLE PRECISION DEFAULT 0,
                    resolved_at     TIMESTAMPTZ,
                    created_at      TIMESTAMPTZ DEFAULT NOW()
                );
                -- Migración: agregar strategy para diferenciar score vs early_entry
                ALTER TABLE autotrades ADD COLUMN IF NOT EXISTS strategy TEXT DEFAULT 'score';
                CREATE INDEX IF NOT EXISTS idx_autotrades_created
                    ON autotrades(created_at);
                CREATE INDEX IF NOT EXISTS idx_autotrades_resolved
                    ON autotrades(resolved);
                CREATE INDEX IF NOT EXISTS idx_autotrades_cid
                    ON autotrades(condition_id);

                CREATE TABLE IF NOT EXISTS alert_autotrades (
                    id              SERIAL PRIMARY KEY,
                    condition_id    TEXT NOT NULL,
                    order_id        TEXT DEFAULT '',
                    market_slug     TEXT DEFAULT '',
                    market_question TEXT DEFAULT '',
                    wallet_address  TEXT DEFAULT '',
                    insider_side    TEXT DEFAULT '',
                    insider_outcome TEXT DEFAULT '',
                    insider_size    DOUBLE PRECISION DEFAULT 0,
                    alert_score     INTEGER DEFAULT 0,
                    triggers        TEXT DEFAULT '',
                    side            TEXT DEFAULT 'BUY',
                    outcome         TEXT DEFAULT '',
                    price           DOUBLE PRECISION,
                    size_usd        DOUBLE PRECISION,
                    shares          DOUBLE PRECISION,
                    token_id        TEXT,
                    category        TEXT DEFAULT '',
                    wallet_hit_rate DOUBLE PRECISION DEFAULT 0,
                    status          TEXT DEFAULT 'filled',
                    error           TEXT,
                    resolved        BOOLEAN DEFAULT FALSE,
                    result          TEXT,
                    pnl             DOUBLE PRECISION DEFAULT 0,
                    resolved_at     TIMESTAMPTZ,
                    created_at      TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_alert_autotrades_created
                    ON alert_autotrades(created_at);
                CREATE INDEX IF NOT EXISTS idx_alert_autotrades_resolved
                    ON alert_autotrades(resolved);
                CREATE INDEX IF NOT EXISTS idx_alert_autotrades_cid
                    ON alert_autotrades(condition_id);
            """)

            # Tabla de usuarios
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id          SERIAL PRIMARY KEY,
                    username    TEXT UNIQUE NOT NULL,
                    email       TEXT,
                    password_hash TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    last_login  TIMESTAMPTZ
                );
                CREATE TABLE IF NOT EXISTS user_sessions (
                    token       TEXT PRIMARY KEY,
                    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    expires_at  TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_expires ON user_sessions(expires_at);
            """)
            # === Tablas v8.0 ===
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_journal (
                    id          SERIAL PRIMARY KEY,
                    trade_type  TEXT NOT NULL,
                    trade_id    INTEGER,
                    note        TEXT DEFAULT '',
                    tags        TEXT DEFAULT '',
                    user_id     INTEGER DEFAULT 1,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS bankroll_history (
                    id          SERIAL PRIMARY KEY,
                    balance     DOUBLE PRECISION NOT NULL,
                    daily_pnl   DOUBLE PRECISION DEFAULT 0,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS mm_orders (
                    id          SERIAL PRIMARY KEY,
                    market_id   TEXT NOT NULL,
                    side        TEXT NOT NULL,
                    price       DOUBLE PRECISION NOT NULL,
                    size        DOUBLE PRECISION NOT NULL,
                    status      TEXT DEFAULT 'paper',
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS push_notifications (
                    id          SERIAL PRIMARY KEY,
                    user_id     INTEGER DEFAULT 1,
                    type        TEXT NOT NULL,
                    title       TEXT NOT NULL,
                    body        TEXT DEFAULT '',
                    read        BOOLEAN DEFAULT FALSE,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_push_user ON push_notifications(user_id, read);
                CREATE INDEX IF NOT EXISTS idx_journal_user ON trade_journal(user_id);
            """)

            # Migración multi-tenant: agregar user_id a todas las tablas per-user
            user_migrations = [
                "ALTER TABLE bot_config ADD COLUMN IF NOT EXISTS user_id INTEGER DEFAULT 1",
                "ALTER TABLE alert_autotrades ADD COLUMN IF NOT EXISTS user_id INTEGER DEFAULT 1",
                "ALTER TABLE autotrades ADD COLUMN IF NOT EXISTS user_id INTEGER DEFAULT 1",
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS user_id INTEGER DEFAULT 1",
                "ALTER TABLE bankroll_history ADD COLUMN IF NOT EXISTS user_id INTEGER DEFAULT 1",
                "ALTER TABLE mm_orders ADD COLUMN IF NOT EXISTS user_id INTEGER DEFAULT 1",
                "ALTER TABLE crypto_signals ADD COLUMN IF NOT EXISTS user_id INTEGER DEFAULT 1",
                # Eliminar PK original de bot_config para permitir multi-tenant
                "ALTER TABLE bot_config DROP CONSTRAINT IF EXISTS bot_config_pkey",
                # Índice único compuesto para config por usuario
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_config_key_user ON bot_config(key, user_id)",
                # Índices per-user para queries rápidas
                "CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_autotrades_user ON autotrades(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_alert_autotrades_user ON alert_autotrades(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_bankroll_user ON bankroll_history(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_mm_orders_user ON mm_orders(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_crypto_signals_user ON crypto_signals(user_id)",
                # Quitar UNIQUE de email (permite múltiples usuarios sin email)
                "DROP INDEX IF EXISTS users_email_key",
            ]
            for m in user_migrations:
                try:
                    await conn.execute(m)
                except Exception:
                    pass

    # ── Auth ───────────────────────────────────────────────────────────

    # Config inicial por defecto para usuarios nuevos
    _DEFAULT_USER_CONFIG = {
        "min_size_usd": "50",
        "alert_threshold": "5",
        "large_size_usd": "200",
        "cooldown_hours": "6",
        "feature_orderbook_depth": "False",
        "feature_market_classification": "False",
        "feature_wallet_baskets": "False",
        "feature_sniper_dbscan": "False",
        "feature_crypto_arb": "False",
        "crypto_arb_min_score": "0.40",
        "crypto_arb_strategy": "score",
        "at_enabled": "false",
        "at_bet_size": "10",
        "at_min_edge": "5.0",
        "at_min_confidence": "65",
        "at_max_positions": "3",
        "at_max_daily": "10",
        "at_stop_loss": "15.0",
        "at_take_profit": "25.0",
        "aat_enabled": "false",
        "aat_bet_size": "10",
        "aat_min_score": "8",
        "aat_max_positions": "3",
    }

    async def create_user(self, username: str, password: str, email: str = "", display_name: str = "") -> dict:
        """Crear usuario con password hasheado + config inicial."""
        import hashlib, secrets
        salt = secrets.token_hex(16)
        pw_hash = hashlib.sha256((salt + password).encode()).hexdigest() + ":" + salt
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow("""
                    INSERT INTO users (username, email, password_hash, display_name)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id, username, display_name, created_at
                """, username.lower().strip(), email.strip() or None, pw_hash, display_name or username)
                # Crear config inicial para el nuevo usuario
                new_uid = row["id"]
                if new_uid != 1:
                    for k, v in self._DEFAULT_USER_CONFIG.items():
                        await conn.execute("""
                            INSERT INTO bot_config (key, value, user_id) VALUES ($1, $2, $3)
                            ON CONFLICT (key, user_id) DO NOTHING
                        """, k, v, new_uid)
                return {"id": new_uid, "username": row["username"], "display_name": row["display_name"]}
            except asyncpg.UniqueViolationError:
                return {"error": "Usuario ya existe"}

    async def verify_user(self, username: str, password: str) -> dict:
        """Verificar credenciales y retornar usuario."""
        import hashlib
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE username = $1", username.lower().strip()
            )
            if not row:
                return {"error": "Usuario no encontrado"}
            stored = row["password_hash"]
            pw_hash_str, salt = stored.rsplit(":", 1)
            check = hashlib.sha256((salt + password).encode()).hexdigest()
            if check != pw_hash_str:
                return {"error": "Contraseña incorrecta"}
            await conn.execute(
                "UPDATE users SET last_login = NOW() WHERE id = $1", row["id"]
            )
            return {"id": row["id"], "username": row["username"], "display_name": row["display_name"] or row["username"]}

    async def create_session(self, user_id: int) -> str:
        """Crear sesión y retornar token."""
        import secrets
        token = secrets.token_urlsafe(32)
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_sessions (token, user_id, expires_at)
                VALUES ($1, $2, NOW() + INTERVAL '30 days')
            """, token, user_id)
        return token

    async def get_session_user(self, token: str) -> dict:
        """Obtener usuario de una sesión válida."""
        if not token:
            return {}
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT u.id, u.username, u.display_name, u.email
                FROM user_sessions s JOIN users u ON s.user_id = u.id
                WHERE s.token = $1 AND s.expires_at > NOW()
            """, token)
            if row:
                return dict(row)
        return {}

    async def delete_session(self, token: str):
        """Cerrar sesión."""
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM user_sessions WHERE token = $1", token)

    async def get_all_active_users(self) -> list[dict]:
        """Obtener todos los usuarios activos (con sesión válida o config guardada)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT u.id, u.username, u.display_name
                FROM users u
                ORDER BY u.id
            """)
            return [dict(r) for r in rows]

    async def get_config_for_user(self, user_id: int, keys: list) -> dict:
        """Obtener config específica de un usuario."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM bot_config WHERE key = ANY($1) AND user_id = $2",
                keys, user_id
            )
            result = {r["key"]: r["value"] for r in rows}
            # Fallback a config global (user_id=1) si no hay config de usuario
            if len(result) < len(keys):
                missing = [k for k in keys if k not in result]
                rows2 = await conn.fetch(
                    "SELECT key, value FROM bot_config WHERE key = ANY($1) AND user_id = 1",
                    missing
                )
                for r in rows2:
                    result[r["key"]] = r["value"]
            return result

    async def set_config_for_user(self, user_id: int, data: dict):
        """Guardar config para un usuario específico."""
        async with self._pool.acquire() as conn:
            for key, value in data.items():
                await conn.execute("""
                    INSERT INTO bot_config (key, value, user_id)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (key, user_id)
                    DO UPDATE SET value = $2
                """, key, str(value), user_id)

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
                    first_seen=row["first_seen"] or datetime.now(timezone.utc),
                    last_seen=row["last_seen"] or datetime.now(timezone.utc),
                    total_volume=row["total_volume"] or 0,
                    avg_trade_size=row["avg_trade_size"] or 0,
                    markets_traded=row["markets_traded"] or 0,
                    win_count=row["win_count"] or 0,
                    loss_count=row["loss_count"] or 0,
                )
        return None

    async def update_wallet_stats(self, trade: Trade):
        addr = trade.wallet_address.lower()
        now = datetime.now(timezone.utc)
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
                    last_updated=row["last_updated"] or datetime.now(timezone.utc),
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
            """, condition_id, n, total, avg, median, p90, p95, datetime.now(timezone.utc))

    # ── Alerts ────────────────────────────────────────────────────────

    async def should_alert(self, wallet: str, market_id: str, cooldown_hours: int = 6, user_id: int = 1) -> bool:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM alerts
                WHERE wallet_address = $1 AND market_id = $2 AND created_at > $3 AND user_id = $4
            """, wallet.lower(), market_id, cutoff, user_id)
            return row is None

    async def record_alert(
        self, wallet: str, market_id: str, market_question: str,
        market_slug: str, side: str, outcome: str, size: float,
        price: float, score: int, triggers: list[str],
        cluster_wallets: list[str], days_to_close: Optional[float],
        wallet_hit_rate: Optional[float], user_id: int = 1,
    ) -> int:
        """Registrar alerta y devolver ID."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO alerts
                (wallet_address, market_id, market_question, market_slug,
                 side, outcome, size, price, score, triggers,
                 cluster_wallets, days_to_close, wallet_hit_rate, user_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                RETURNING id
            """,
                wallet.lower(), market_id, market_question, market_slug,
                side, outcome, size, price, score,
                "||".join(triggers), ",".join(cluster_wallets),
                days_to_close, wallet_hit_rate, user_id,
            )
            return row["id"] if row else 0

    async def copy_alerts_to_new_user(self, new_user_id: int):
        """Copiar todas las alertas del admin (user_id=1) al nuevo usuario.
        Esto le da data histórica inmediata al registrarse.
        """
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO alerts
                (wallet_address, market_id, market_question, market_slug,
                 side, outcome, size, price, score, triggers,
                 cluster_wallets, days_to_close, wallet_hit_rate, user_id,
                 created_at, price_at_alert, is_copy_trade, result, pnl,
                 resolved, resolution, was_correct, resolved_at,
                 paper_pnl, paper_shares, exit_price, exit_type, exit_at)
                SELECT wallet_address, market_id, market_question, market_slug,
                       side, outcome, size, price, score, triggers,
                       cluster_wallets, days_to_close, wallet_hit_rate, $1,
                       created_at, price_at_alert, is_copy_trade, result, pnl,
                       resolved, resolution, was_correct, resolved_at,
                       paper_pnl, paper_shares, exit_price, exit_type, exit_at
                FROM alerts WHERE user_id = 1
            """, new_user_id)
            copied = await conn.fetchval(
                "SELECT COUNT(*) FROM alerts WHERE user_id = $1", new_user_id
            )
            if copied:
                logger.info(f"alerts_copied new_user={new_user_id} count={copied}")

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

            # Actualizar alertas de este mercado con was_correct + paper PnL
            # PnL formula: shares = size / price, won → shares*(1-price), lost → -size
            await conn.execute("""
                UPDATE alerts SET resolved = TRUE, resolution = $1,
                    resolved_at = NOW(),
                    exit_type = COALESCE(exit_type, 'resolution'),
                    was_correct = CASE
                        WHEN side = 'BUY' THEN (outcome = $1)
                        WHEN side = 'SELL' THEN (outcome != $1)
                        ELSE FALSE
                    END,
                    paper_shares = CASE WHEN COALESCE(price, 0) > 0
                        THEN size / price ELSE 0 END,
                    exit_price = CASE
                        WHEN side = 'BUY' AND outcome = $1 THEN 1.0
                        WHEN side = 'BUY' AND outcome != $1 THEN 0.0
                        WHEN side = 'SELL' AND outcome != $1 THEN 1.0
                        WHEN side = 'SELL' AND outcome = $1 THEN 0.0
                        ELSE 0.0 END,
                    exit_at = NOW(),
                    paper_pnl = CASE WHEN COALESCE(price, 0) > 0 THEN
                        CASE
                            WHEN side = 'BUY' AND outcome = $1
                                THEN (size / price) * (1.0 - price)
                            WHEN side = 'BUY' AND outcome != $1
                                THEN -size
                            WHEN side = 'SELL' AND outcome != $1
                                THEN (size / price) * (1.0 - price)
                            WHEN side = 'SELL' AND outcome = $1
                                THEN -size
                            ELSE 0 END
                        ELSE 0 END
                WHERE market_id = $2 AND resolved = FALSE
                    AND exit_type IS NULL  -- no sobreescribir wallet_exit
            """, resolution, condition_id)

            # Resolver alertas ya cerradas por wallet (actualizar was_correct pero no PnL)
            await conn.execute("""
                UPDATE alerts SET resolved = TRUE, resolution = $1,
                    was_correct = CASE
                        WHEN side = 'BUY' THEN (outcome = $1)
                        WHEN side = 'SELL' THEN (outcome != $1)
                        ELSE FALSE
                    END
                WHERE market_id = $2 AND resolved = FALSE
                    AND exit_type = 'wallet_exit'
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

    # ── Paper Trading PnL ────────────────────────────────────────────

    async def check_wallet_exit(self, wallet: str, market_id: str,
                                outcome: str, exit_price: float) -> int:
        """Marcar alertas abiertas de esta wallet como cerradas por wallet exit.
        Retorna cantidad de alertas cerradas."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, size, price FROM alerts
                WHERE wallet_address = $1 AND market_id = $2
                  AND outcome = $3 AND side = 'BUY'
                  AND resolved = FALSE AND exit_type IS NULL
            """, wallet.lower(), market_id, outcome)

            closed = 0
            for r in rows:
                entry_price = r["price"] or 0
                if entry_price <= 0:
                    continue
                shares = r["size"] / entry_price
                pnl = shares * (exit_price - entry_price)
                await conn.execute("""
                    UPDATE alerts SET
                        exit_type = 'wallet_exit',
                        exit_price = $1,
                        exit_at = NOW(),
                        paper_shares = $2,
                        paper_pnl = $3
                    WHERE id = $4
                """, exit_price, shares, pnl, r["id"])
                closed += 1
            return closed

    async def get_paper_trading_stats(self, user_id: int = 1) -> dict:
        """Estadísticas de paper trading basadas en alertas."""
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT
                        COUNT(*) as total_alerts,
                        COUNT(*) FILTER (WHERE side = 'BUY') as buy_alerts,
                        COUNT(*) FILTER (WHERE paper_pnl IS NOT NULL) as closed_alerts,
                        COUNT(*) FILTER (WHERE paper_pnl IS NOT NULL AND paper_pnl > 0) as wins,
                        COUNT(*) FILTER (WHERE paper_pnl IS NOT NULL AND paper_pnl <= 0) as losses,
                        COUNT(*) FILTER (WHERE resolved = FALSE AND exit_type IS NULL AND side = 'BUY') as open_positions,
                        COALESCE(SUM(paper_pnl) FILTER (WHERE paper_pnl IS NOT NULL), 0) as total_pnl,
                        COALESCE(SUM(size) FILTER (WHERE side = 'BUY'), 0) as total_invested,
                        COALESCE(MAX(paper_pnl) FILTER (WHERE paper_pnl IS NOT NULL), 0) as best_trade,
                        COALESCE(MIN(paper_pnl) FILTER (WHERE paper_pnl IS NOT NULL), 0) as worst_trade,
                        COALESCE(AVG(paper_pnl) FILTER (WHERE paper_pnl IS NOT NULL), 0) as avg_pnl,
                        COUNT(*) FILTER (WHERE exit_type = 'wallet_exit') as wallet_exits,
                        COUNT(*) FILTER (WHERE exit_type = 'resolution') as resolution_exits
                    FROM alerts WHERE user_id = $1
                """, user_id)
                if not row:
                    return {}
                total_closed = int(row["wins"] or 0) + int(row["losses"] or 0)
                win_rate = (int(row["wins"] or 0) / total_closed * 100) if total_closed > 0 else 0
                total_invested = float(row["total_invested"] or 0)
                total_pnl = float(row["total_pnl"] or 0)
                roi = (total_pnl / total_invested * 100) if total_invested > 0 else 0
                return {
                    "total_alerts": int(row["total_alerts"] or 0),
                    "buy_alerts": int(row["buy_alerts"] or 0),
                    "closed_alerts": total_closed,
                    "open_positions": int(row["open_positions"] or 0),
                    "wins": int(row["wins"] or 0),
                    "losses": int(row["losses"] or 0),
                    "win_rate": round(win_rate, 1),
                    "total_pnl": round(total_pnl, 2),
                    "total_invested": round(total_invested, 2),
                    "roi_pct": round(roi, 1),
                    "best_trade": round(float(row["best_trade"] or 0), 2),
                    "worst_trade": round(float(row["worst_trade"] or 0), 2),
                    "avg_pnl": round(float(row["avg_pnl"] or 0), 2),
                    "wallet_exits": int(row["wallet_exits"] or 0),
                    "resolution_exits": int(row["resolution_exits"] or 0),
                }
        except Exception as e:
            print(f"[DB] get_paper_trading_stats error: {e}", flush=True)
            return {}

    async def get_paper_pnl_history(self, days: int = 30, user_id: int = 1) -> list[dict]:
        """Historial diario de paper PnL para gráfico."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT DATE(COALESCE(exit_at, resolved_at)) as date,
                           SUM(paper_pnl) as daily_pnl,
                           COUNT(*) as trades,
                           SUM(CASE WHEN paper_pnl > 0 THEN 1 ELSE 0 END) as wins,
                           SUM(CASE WHEN paper_pnl <= 0 THEN 1 ELSE 0 END) as losses
                    FROM alerts
                    WHERE paper_pnl IS NOT NULL
                      AND COALESCE(exit_at, resolved_at) > NOW() - INTERVAL '1 day' * $1
                      AND user_id = $2
                    GROUP BY DATE(COALESCE(exit_at, resolved_at))
                    ORDER BY date ASC
                """, days, user_id)
                result = []
                cumulative = 0
                for r in rows:
                    daily = float(r["daily_pnl"] or 0)
                    cumulative += daily
                    result.append({
                        "date": r["date"].isoformat() if r["date"] else "",
                        "pnl": round(daily, 2),
                        "cumulative_pnl": round(cumulative, 2),
                        "trades": int(r["trades"] or 0),
                        "wins": int(r["wins"] or 0),
                        "losses": int(r["losses"] or 0),
                    })
                return result
        except Exception as e:
            print(f"[DB] get_paper_pnl_history error: {e}", flush=True)
            return []

    async def get_paper_wallet_ranking(self, limit: int = 20, user_id: int = 1) -> list[dict]:
        """Ranking de wallets alertadas por paper PnL."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT
                        a.wallet_address as address,
                        COALESCE(w.name, w.pseudonym) as name,
                        COUNT(*) as total_trades,
                        AVG(a.score) as avg_score,
                        SUM(CASE WHEN a.paper_pnl > 0 THEN 1 ELSE 0 END) as wins,
                        SUM(CASE WHEN a.paper_pnl <= 0 THEN 1 ELSE 0 END) as losses,
                        COALESCE(SUM(a.paper_pnl), 0) as total_pnl,
                        COALESCE(SUM(a.size), 0) as volume
                    FROM alerts a
                    LEFT JOIN wallets w ON a.wallet_address = w.address
                    WHERE a.paper_pnl IS NOT NULL AND a.user_id = $2
                    GROUP BY a.wallet_address, w.name, w.pseudonym
                    ORDER BY SUM(a.paper_pnl) DESC
                    LIMIT $1
                """, limit, user_id)
                result = []
                for r in rows:
                    total = int(r["wins"] or 0) + int(r["losses"] or 0)
                    result.append({
                        "address": r["address"],
                        "name": r["name"],
                        "total_trades": int(r["total_trades"] or 0),
                        "score": round(float(r["avg_score"] or 0), 1),
                        "wins": int(r["wins"] or 0),
                        "losses": int(r["losses"] or 0),
                        "win_rate": round(int(r["wins"] or 0) / max(total, 1) * 100, 1),
                        "total_pnl": round(float(r["total_pnl"] or 0), 2),
                        "volume": round(float(r["volume"] or 0), 0),
                    })
                return result
        except Exception as e:
            print(f"[DB] get_paper_wallet_ranking error: {e}", flush=True)
            return []

    async def get_paper_recent_trades(self, limit: int = 50, user_id: int = 1) -> list[dict]:
        """Trades recientes del paper trading (alertas BUY con PnL info)."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT
                        a.id, a.created_at, a.market_question, a.market_slug,
                        a.wallet_address, a.side, a.outcome, a.size, a.price,
                        a.score, a.resolved, a.was_correct, a.paper_pnl,
                        a.paper_shares, a.exit_price, a.exit_type, a.exit_at,
                        a.price_at_alert, a.price_latest,
                        COALESCE(w.name, w.pseudonym) as wallet_name
                    FROM alerts a
                    LEFT JOIN wallets w ON a.wallet_address = w.address
                    WHERE a.side = 'BUY' AND a.user_id = $2
                    ORDER BY a.created_at DESC
                    LIMIT $1
                """, limit, user_id)
                return [_serialize_row(r) for r in rows]
        except Exception as e:
            print(f"[DB] get_paper_recent_trades error: {e}", flush=True)
            return []

    async def get_open_paper_alerts(self, user_id: int = 1) -> list[dict]:
        """Alertas BUY abiertas (posiciones paper sin cerrar)."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT
                        a.id, a.created_at, a.market_question, a.market_slug,
                        a.market_id, a.wallet_address, a.outcome, a.size, a.price,
                        a.score, a.price_at_alert, a.price_latest,
                        COALESCE(w.name, w.pseudonym) as wallet_name
                    FROM alerts a
                    LEFT JOIN wallets w ON a.wallet_address = w.address
                    WHERE a.side = 'BUY' AND a.resolved = FALSE
                      AND a.exit_type IS NULL AND a.user_id = $1
                    ORDER BY a.created_at DESC
                """, user_id)
                result = []
                for r in rows:
                    row = _serialize_row(r)
                    # Calcular unrealized PnL con price_latest
                    entry_p = float(r["price"] or 0)
                    latest_p = float(r["price_latest"] or r["price_at_alert"] or 0)
                    outcome = r.get("outcome", "Yes")
                    size = float(r["size"] or 0)
                    if entry_p > 0 and latest_p > 0:
                        shares = size / entry_p
                        # price_latest es siempre el precio de Yes
                        # Si outcome = No, precio actual del No = 1.0 - yes_price
                        current_p = latest_p if outcome == "Yes" else (1.0 - latest_p)
                        row["paper_shares"] = round(shares, 2)
                        row["unrealized_pnl"] = round(shares * (current_p - entry_p), 2)
                        row["current_price"] = round(current_p, 4)
                    result.append(row)
                return result
        except Exception as e:
            print(f"[DB] get_open_paper_alerts error: {e}", flush=True)
            return []

    async def get_paper_pnl_by_score(self, user_id: int = 1) -> list[dict]:
        """PnL promedio agrupado por rango de score (para análisis de señales)."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT
                        CASE
                            WHEN score >= 15 THEN '15+'
                            WHEN score >= 12 THEN '12-14'
                            WHEN score >= 9 THEN '9-11'
                            WHEN score >= 6 THEN '6-8'
                            ELSE '3-5'
                        END as score_range,
                        COUNT(*) as count,
                        AVG(paper_pnl) as avg_pnl,
                        SUM(paper_pnl) as total_pnl,
                        SUM(CASE WHEN paper_pnl > 0 THEN 1 ELSE 0 END) as wins,
                        SUM(CASE WHEN paper_pnl <= 0 THEN 1 ELSE 0 END) as losses
                    FROM alerts
                    WHERE paper_pnl IS NOT NULL AND side = 'BUY' AND user_id = $1
                    GROUP BY score_range
                    ORDER BY MIN(score) DESC
                """, user_id)
                return [{
                    "score_range": r["score_range"],
                    "count": int(r["count"] or 0),
                    "avg_pnl": round(float(r["avg_pnl"] or 0), 2),
                    "total_pnl": round(float(r["total_pnl"] or 0), 2),
                    "wins": int(r["wins"] or 0),
                    "losses": int(r["losses"] or 0),
                    "win_rate": round(int(r["wins"] or 0) / max(int(r["count"] or 1), 1) * 100, 1),
                } for r in rows]
        except Exception as e:
            print(f"[DB] get_paper_pnl_by_score error: {e}", flush=True)
            return []

    # ── Clustering ────────────────────────────────────────────────────

    async def find_cluster_wallets(self, market_id: str, side: str,
                                   outcome: str, window_minutes: int = 30,
                                   min_size: float = 1000) -> list[str]:
        """Encontrar wallets que apostaron en el mismo lado en una ventana reciente."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
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

    async def _get_user_alert_filters(self, user_id: int) -> tuple:
        """Obtener min_size_usd y alert_threshold del usuario para filtrar alertas."""
        cfg = await self.get_config_bulk(
            ["min_size_usd", "alert_threshold"], user_id=user_id
        )
        min_size = float(cfg.get("min_size_usd", 50))
        min_score = int(float(cfg.get("alert_threshold", 5)))
        return min_size, min_score

    async def get_dashboard_stats(self, user_id: int = 1) -> dict:
        min_size, min_score = await self._get_user_alert_filters(user_id)
        base_filter = "user_id = $1 AND size >= $2 AND score >= $3"
        async with self._pool.acquire() as conn:
            total_alerts = await conn.fetchval(
                f"SELECT COUNT(*) FROM alerts WHERE {base_filter}", user_id, min_size, min_score)
            resolved_alerts = await conn.fetchval(
                f"SELECT COUNT(*) FROM alerts WHERE resolved = TRUE AND {base_filter}", user_id, min_size, min_score)
            correct_alerts = await conn.fetchval(
                f"SELECT COUNT(*) FROM alerts WHERE was_correct = TRUE AND {base_filter}", user_id, min_size, min_score)
            total_trades = await conn.fetchval("SELECT COUNT(*) FROM trades")
            unique_wallets = await conn.fetchval(
                f"SELECT COUNT(DISTINCT wallet_address) FROM alerts WHERE {base_filter}", user_id, min_size, min_score)
            alerts_24h = await conn.fetchval(
                f"SELECT COUNT(*) FROM alerts WHERE created_at > NOW() - INTERVAL '24 hours' AND {base_filter}",
                user_id, min_size, min_score)
            avg_score = await conn.fetchval(
                f"SELECT COALESCE(AVG(score), 0) FROM alerts WHERE {base_filter}", user_id, min_size, min_score)
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

    async def get_recent_alerts(self, limit: int = 50, user_id: int = 1) -> list[dict]:
        min_size, min_score = await self._get_user_alert_filters(user_id)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM alerts
                WHERE user_id = $1 AND size >= $3 AND score >= $4
                ORDER BY created_at DESC LIMIT $2
            """, user_id, limit, min_size, min_score)
            result = []
            for r in rows:
                d = dict(r)
                for k, v in d.items():
                    if isinstance(v, datetime):
                        d[k] = v.isoformat()
                result.append(d)
            return result

    async def get_top_wallets(self, limit: int = 20, user_id: int = 1) -> list[dict]:
        min_size, min_score = await self._get_user_alert_filters(user_id)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT w.address,
                       w.total_trades, w.total_volume, w.avg_trade_size,
                       w.win_count, w.loss_count, w.markets_traded,
                       COUNT(a.id) as alert_count,
                       MAX(a.score) as max_score
                FROM wallets w
                JOIN alerts a ON a.wallet_address = w.address
                    AND a.user_id = $2 AND a.size >= $3 AND a.score >= $4
                GROUP BY w.address, w.total_trades, w.total_volume,
                         w.avg_trade_size, w.win_count, w.loss_count, w.markets_traded
                ORDER BY alert_count DESC, max_score DESC
                LIMIT $1
            """, limit, user_id, min_size, min_score)
            return [dict(r) for r in rows]

    async def get_alerts_by_day(self, days: int = 30, user_id: int = 1) -> list[dict]:
        min_size, min_score = await self._get_user_alert_filters(user_id)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT DATE(created_at) as day,
                       COUNT(*) as count,
                       AVG(score) as avg_score,
                       SUM(CASE WHEN was_correct THEN 1 ELSE 0 END) as correct
                FROM alerts
                WHERE created_at > NOW() - ($1 || ' days')::INTERVAL
                  AND user_id = $2 AND size >= $3 AND score >= $4
                GROUP BY DATE(created_at)
                ORDER BY day
            """, str(days), user_id, min_size, min_score)
            return [{"day": str(r["day"]), "count": r["count"],
                     "avg_score": round(float(r["avg_score"] or 0), 1),
                     "correct": r["correct"] or 0} for r in rows]

    async def get_score_distribution(self, user_id: int = 1) -> list[dict]:
        min_size, min_score = await self._get_user_alert_filters(user_id)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT score, COUNT(*) as count
                FROM alerts
                WHERE user_id = $1 AND size >= $2 AND score >= $3
                GROUP BY score ORDER BY score
            """, user_id, min_size, min_score)
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

    # Keys sensibles que NUNCA deben heredarse del admin a otros usuarios
    _SENSITIVE_KEYS = {
        "at_api_key", "at_api_secret", "at_private_key", "at_passphrase",
        "aat_api_key", "aat_api_secret", "aat_private_key", "aat_passphrase",
        "telegram_bot_token", "telegram_chat_ids",
    }

    async def get_config(self, user_id: int = 1) -> dict:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM bot_config WHERE user_id = $1", user_id)
            result = {r["key"]: r["value"] for r in rows}
            # Fallback a config global (user_id=1) para keys faltantes
            # PERO excluir credenciales sensibles — cada usuario debe tener las suyas
            if user_id != 1:
                rows2 = await conn.fetch("SELECT key, value FROM bot_config WHERE user_id = 1")
                for r in rows2:
                    if r["key"] not in result and r["key"] not in self._SENSITIVE_KEYS:
                        result[r["key"]] = r["value"]
            return result

    async def get_config_bulk(self, keys: list[str], user_id: int = 1) -> dict:
        """Obtener múltiples valores de config por lista de keys."""
        all_config = await self.get_config(user_id=user_id)
        return {k: all_config[k] for k in keys if k in all_config}

    async def set_config(self, data: dict, user_id: int = 1):
        """Guardar config (acepta dict o key/value para retrocompat)."""
        if isinstance(data, str):
            # Retrocompat: set_config(key, value) → set_config({key: value})
            # No debería pasar pero por seguridad
            return
        async with self._pool.acquire() as conn:
            for k, v in data.items():
                await conn.execute("""
                    INSERT INTO bot_config (key, value, user_id) VALUES ($1, $2, $3)
                    ON CONFLICT (key, user_id) DO UPDATE SET value = $2
                """, k, str(v), user_id)

    async def set_config_bulk(self, data: dict, user_id: int = 1):
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for k, v in data.items():
                    await conn.execute("""
                        INSERT INTO bot_config (key, value, user_id) VALUES ($1, $2, $3)
                        ON CONFLICT (key, user_id) DO UPDATE SET value = $2
                    """, k, str(v), user_id)

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

    async def get_alert_category_distribution(self, user_id: int = 1) -> list[dict]:
        min_size, min_score = await self._get_user_alert_filters(user_id)
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
                WHERE a.user_id = $1 AND a.size >= $2 AND a.score >= $3
                GROUP BY t.market_category
                ORDER BY alert_count DESC
            """, user_id, min_size, min_score)
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

    async def get_alerts_for_latest_price(self, limit: int = 30) -> list[dict]:
        """Obtener alertas no resueltas que necesitan update de price_latest (cada ~1h)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, market_id, outcome, side, price_at_alert
                FROM alerts
                WHERE resolved = FALSE
                  AND price_at_alert IS NOT NULL
                  AND (price_latest_at IS NULL OR price_latest_at < NOW() - INTERVAL '55 minutes')
                ORDER BY created_at DESC
                LIMIT $1
            """, limit)
            return [dict(r) for r in rows]

    async def update_alert_price_latest(self, alert_id: int, price: float):
        """Actualizar price_latest con precio actual del mercado."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE alerts SET price_latest = $1, price_latest_at = NOW() WHERE id = $2",
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

    async def record_alert_for_all_users(self, **kwargs) -> int:
        """Registrar la misma alerta para TODOS los usuarios registrados.
        Retorna el alert_id del admin (user_id=1).
        """
        price_at_alert = kwargs.pop("price_at_alert", None)
        is_copy_trade = kwargs.pop("is_copy_trade", False)

        # Obtener todos los user_ids
        async with self._pool.acquire() as conn:
            user_ids = [r["id"] for r in await conn.fetch("SELECT id FROM users ORDER BY id")]

        if not user_ids:
            user_ids = [1]

        admin_alert_id = 0
        for uid in user_ids:
            kwargs["user_id"] = uid
            alert_id = await self.record_alert(**kwargs)
            if uid == 1:
                admin_alert_id = alert_id
            if alert_id and (price_at_alert is not None or is_copy_trade):
                async with self._pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE alerts SET price_at_alert = $1, is_copy_trade = $2 WHERE id = $3
                    """, price_at_alert, is_copy_trade, alert_id)

        return admin_alert_id

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

    async def get_leaderboard(self, limit: int = 30, sort_by: str = "pnl", user_id: int = 1) -> list[dict]:
        min_size, min_score = await self._get_user_alert_filters(user_id)
        order = {
            "pnl": "total_pnl DESC",
            "roi": "roi_pct DESC",
            "score": "smart_money_score DESC",
            "winrate": "(win_count::float / NULLIF(win_count + loss_count, 0)) DESC",
        }.get(sort_by, "total_pnl DESC")

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(f"""
                SELECT w.address, w.name, w.pseudonym, w.profile_image,
                       w.total_trades, w.total_volume, w.avg_trade_size,
                       w.win_count, w.loss_count, w.markets_traded,
                       w.total_pnl, w.total_cost, w.roi_pct,
                       w.smart_money_score, w.correct_markets,
                       w.is_watchlisted,
                       w.on_chain_first_tx, w.on_chain_funded_by,
                       w.on_chain_age_days, w.on_chain_tx_count,
                       w.on_chain_usdc_in, w.on_chain_usdc_out,
                       w.on_chain_erc1155_transfers
                FROM wallets w
                WHERE (w.win_count + w.loss_count) > 0
                  AND w.address IN (
                      SELECT DISTINCT wallet_address FROM alerts
                      WHERE user_id = $2 AND size >= $3 AND score >= $4
                  )
                ORDER BY {order}
                LIMIT $1
            """, limit, user_id, min_size, min_score)
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
                JOIN markets_tracked m ON m.condition_id = t.market_id
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
                    JOIN markets_tracked m ON m.condition_id = t.market_id
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
        """Registrar señal crypto arb y devolver ID. Rechaza duplicados por condition_id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO crypto_signals
                (coin, direction, spot_change_pct, poly_odds, fair_odds,
                 confidence, edge_pct, condition_id, market_question,
                 spot_price, time_remaining_sec, paper_bet_size, event_slug, score_details, strategy)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                ON CONFLICT (condition_id) DO NOTHING
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
                json.dumps(signal.get("score_details", {})),
                signal.get("strategy", "score"),
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


    # ── Autotrades ──────────────────────────────────────────────────────

    async def record_autotrade(self, trade: dict, user_id: int = 1):
        """Guardar un trade ejecutado (o rechazado) por el autotrader."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO autotrades
                    (condition_id, order_id, coin, direction, side, price, size_usd,
                     shares, token_id, edge_pct, confidence, event_slug,
                     order_type, status, error, user_id, strategy)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
            """,
                trade.get("condition_id", ""),
                trade.get("order_id", ""),
                trade.get("coin", ""),
                trade.get("direction", ""),
                trade.get("side", "BUY"),
                trade.get("price", 0),
                trade.get("size_usd", 0),
                trade.get("shares", 0),
                trade.get("token_id", ""),
                trade.get("edge_pct", 0),
                trade.get("confidence", 0),
                trade.get("event_slug", ""),
                trade.get("order_type", "limit"),
                trade.get("status", "filled"),
                trade.get("error"),
                user_id,
                trade.get("strategy", "score"),
            )

    async def resolve_autotrade(self, condition_id: str, result: str, pnl: float):
        """Marcar un autotrade como resuelto."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE autotrades
                SET resolved = TRUE, result = $2, pnl = $3, resolved_at = NOW()
                WHERE condition_id = $1 AND resolved = FALSE
            """, condition_id, result, pnl)

    async def get_autotrades(self, hours: int = 24, limit: int = 100, user_id: int = 1) -> list[dict]:
        """Obtener autotrades recientes."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM autotrades
                WHERE created_at > NOW() - INTERVAL '1 hour' * $1 AND user_id = $3
                ORDER BY created_at DESC LIMIT $2
            """, hours, limit, user_id)
            return [_serialize_row(r) for r in rows]

    async def get_open_autotrades(self, user_id: int = 1) -> list[dict]:
        """Obtener autotrades no resueltos (posiciones abiertas)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM autotrades
                WHERE resolved = FALSE AND status = 'filled' AND user_id = $1
                ORDER BY created_at DESC
            """, user_id)
            return [_serialize_row(r) for r in rows]

    async def get_autotrade_stats(self, user_id: int = 1) -> dict:
        """Estadísticas de autotrading."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT
                    COUNT(*) FILTER (WHERE status = 'filled') as total_trades,
                    COUNT(*) FILTER (WHERE resolved AND result = 'win') as wins,
                    COUNT(*) FILTER (WHERE resolved AND result = 'loss') as losses,
                    COUNT(*) FILTER (WHERE NOT resolved AND status = 'filled') as open_positions,
                    COALESCE(SUM(pnl) FILTER (WHERE resolved), 0) as total_pnl,
                    COALESCE(SUM(size_usd) FILTER (WHERE status = 'filled'), 0) as total_volume,
                    COALESCE(SUM(pnl) FILTER (WHERE resolved AND created_at > NOW() - INTERVAL '24 hours'), 0) as pnl_24h,
                    COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hours' AND status = 'filled') as trades_24h
                FROM autotrades WHERE user_id = $1
            """, user_id)
            total = (row["wins"] or 0) + (row["losses"] or 0)
            return {
                "total_trades": row["total_trades"] or 0,
                "wins": row["wins"] or 0,
                "losses": row["losses"] or 0,
                "open_positions": row["open_positions"] or 0,
                "win_rate": round((row["wins"] / total * 100) if total else 0, 1),
                "total_pnl": round(float(row["total_pnl"] or 0), 2),
                "total_volume": round(float(row["total_volume"] or 0), 2),
                "pnl_24h": round(float(row["pnl_24h"] or 0), 2),
                "trades_24h": row["trades_24h"] or 0,
            }


    # ── Alert AutoTrades (copy-trading de insiders) ────────────────────

    async def record_alert_autotrade(self, trade: dict, user_id: int = 1):
        """Guardar un trade ejecutado por el alert autotrader."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO alert_autotrades
                    (condition_id, order_id, market_slug, market_question,
                     wallet_address, insider_side, insider_outcome, insider_size,
                     alert_score, triggers, side, outcome, price, size_usd,
                     shares, token_id, category, wallet_hit_rate, status, error, user_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21)
            """,
                trade.get("condition_id", ""),
                trade.get("order_id", ""),
                trade.get("market_slug", ""),
                trade.get("market_question", ""),
                trade.get("wallet_address", ""),
                trade.get("insider_side", ""),
                trade.get("insider_outcome", ""),
                trade.get("insider_size", 0),
                trade.get("alert_score", 0),
                trade.get("triggers", ""),
                trade.get("side", "BUY"),
                trade.get("outcome", ""),
                trade.get("price", 0),
                trade.get("size_usd", 0),
                trade.get("shares", 0),
                trade.get("token_id", ""),
                trade.get("category", ""),
                trade.get("wallet_hit_rate", 0),
                trade.get("status", "filled"),
                trade.get("error"),
                user_id,
            )

    async def resolve_alert_autotrade(self, condition_id: str, result: str, pnl: float):
        """Marcar un alert autotrade como resuelto."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE alert_autotrades
                SET resolved = TRUE, result = $2, pnl = $3, resolved_at = NOW()
                WHERE condition_id = $1 AND resolved = FALSE
            """, condition_id, result, pnl)

    async def get_alert_autotrades(self, hours: int = 24, limit: int = 100, user_id: int = 1) -> list[dict]:
        """Obtener alert autotrades recientes (con filtro user_id)."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT * FROM alert_autotrades
                    WHERE created_at > NOW() - INTERVAL '1 hour' * $1 AND user_id = $3
                    ORDER BY created_at DESC LIMIT $2
                """, hours, limit, user_id)
                return [_serialize_row(r) for r in rows]
        except Exception as e:
            print(f"[DB] get_alert_autotrades error: {e}", flush=True)
            return []

    async def get_open_alert_autotrades(self, user_id: int = 1) -> list[dict]:
        """Obtener alert autotrades no resueltos (con filtro user_id)."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT * FROM alert_autotrades
                    WHERE resolved = FALSE AND status = 'filled' AND user_id = $1
                    ORDER BY created_at DESC
                """, user_id)
                return [_serialize_row(r) for r in rows]
        except Exception as e:
            return []

    async def update_alert_autotrade_shares(self, condition_id: str, new_shares: float):
        """Actualizar shares restantes después de partial profit taking."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                UPDATE alert_autotrades
                SET shares = $2, size_usd = $2 * price
                WHERE condition_id = $1 AND resolved = FALSE
            """, condition_id, new_shares)

    async def get_alert_pnl_history(self, days: int = 30, user_id: int = 1) -> list[dict]:
        """Obtener historial de PnL para gráfico de evolución (unificado v10)."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT DATE(resolved_at) as date,
                           SUM(pnl) as daily_pnl,
                           COUNT(*) as trades,
                           SUM(CASE WHEN result IN ('win','take_profit','trailing_stop','partial_tp') THEN 1 ELSE 0 END) as wins,
                           SUM(CASE WHEN result IN ('loss','stop_loss') THEN 1 ELSE 0 END) as losses
                    FROM alert_autotrades
                    WHERE resolved = TRUE AND resolved_at > NOW() - INTERVAL '1 day' * $1 AND user_id = $2
                    GROUP BY DATE(resolved_at)
                    ORDER BY date ASC
                """, days, user_id)
                result = []
                cumulative = 0
                for r in rows:
                    daily = float(r["daily_pnl"] or 0)
                    cumulative += daily
                    result.append({
                        "date": r["date"].isoformat() if r["date"] else "",
                        "pnl": round(daily, 2),
                        "cumulative_pnl": round(cumulative, 2),
                        "trades": int(r["trades"] or 0),
                        "wins": int(r["wins"] or 0),
                        "losses": int(r["losses"] or 0),
                    })
                return result
        except Exception as e:
            print(f"[DB] get_alert_pnl_history error: {e}", flush=True)
            return []

    async def get_alert_wallet_ranking(self, limit: int = 20) -> list[dict]:
        """Ranking de wallets por profit generado en copy-trades (unificado v10)."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT
                        a.wallet_address as address,
                        w.pseudonym as name,
                        COALESCE(w.smart_money_score, 0) as score,
                        COUNT(*) as trades,
                        COUNT(*) FILTER (WHERE a.result IN ('win','take_profit','trailing_stop')) as wins,
                        COALESCE(SUM(a.pnl), 0) as total_pnl,
                        COALESCE(SUM(a.size_usd), 0) as volume
                    FROM alert_autotrades a
                    LEFT JOIN wallets w ON a.wallet_address = w.address
                    WHERE a.status = 'filled' AND a.resolved = TRUE AND a.wallet_address != ''
                    GROUP BY a.wallet_address, w.pseudonym, w.smart_money_score
                    HAVING COUNT(*) >= 2
                    ORDER BY SUM(a.pnl) DESC
                    LIMIT $1
                """, limit)
                result = []
                for r in rows:
                    total = r["trades"]
                    result.append({
                        "address": r["address"],
                        "name": r["name"] or (r["address"][:10] + "..."),
                        "score": round(float(r["score"]), 1),
                        "win_rate": round(r["wins"] / max(total, 1) * 100, 1),
                        "total_pnl": round(float(r["total_pnl"]), 2),
                        "volume": round(float(r["volume"]), 0),
                    })
                return result
        except Exception as e:
            print(f"[DB] get_alert_wallet_ranking error: {e}", flush=True)
            return []

    async def get_backtest_data(self, days: int = 30, min_score: int = 5) -> list[dict]:
        """Datos históricos de alertas para backtesting (unificado v10).
        Usa price_at_alert como entry y calcula exit_price/sim_result."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT
                        a.score, a.size,
                        a.market_category as category,
                        a.price_at_alert as entry_price,
                        a.price_1h, a.price_6h, a.price_24h,
                        a.price_latest,
                        a.was_correct, a.resolved,
                        a.outcome, a.side,
                        COALESCE(a.price_latest, a.price_24h, a.price_6h, a.price_1h) as exit_price,
                        CASE WHEN a.resolved THEN
                            CASE WHEN a.was_correct THEN 'win' ELSE 'loss' END
                            ELSE 'open' END as sim_result,
                        CASE WHEN (w.win_count + w.loss_count) > 0
                            THEN (w.win_count::float / (w.win_count + w.loss_count) * 100)
                            ELSE 0 END as hit_rate
                    FROM alerts a
                    LEFT JOIN wallets w ON a.wallet_address = w.address
                    WHERE a.created_at > NOW() - INTERVAL '1 day' * $1
                      AND a.score >= $2
                      AND a.price_at_alert IS NOT NULL AND a.price_at_alert > 0
                    ORDER BY a.created_at DESC
                    LIMIT 2000
                """, days, min_score)
                return [dict(r) for r in rows]
        except Exception as e:
            print(f"[DB] get_backtest_data error: {e}", flush=True)
            return []

    async def get_alert_autotrade_stats(self, user_id: int = 1) -> dict:
        """Estadísticas de alert autotrading (unificado v10)."""
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT
                        COUNT(*) FILTER (WHERE status = 'filled') as total_trades,
                        COUNT(*) FILTER (WHERE resolved AND result IN ('win','take_profit','trailing_stop')) as wins,
                        COUNT(*) FILTER (WHERE resolved AND result IN ('loss','stop_loss')) as losses,
                        COUNT(*) FILTER (WHERE NOT resolved AND status = 'filled') as open_positions,
                        COALESCE(SUM(pnl) FILTER (WHERE resolved), 0) as total_pnl,
                        COALESCE(SUM(size_usd) FILTER (WHERE status = 'filled'), 0) as total_volume,
                        COALESCE(SUM(pnl) FILTER (WHERE resolved AND created_at > NOW() - INTERVAL '24 hours'), 0) as pnl_24h,
                        COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hours' AND status = 'filled') as trades_24h,
                        MAX(pnl) FILTER (WHERE resolved) as best_trade,
                        MIN(pnl) FILTER (WHERE resolved) as worst_trade
                    FROM alert_autotrades WHERE user_id = $1
                """, user_id)
                total = (row["wins"] or 0) + (row["losses"] or 0)
                return {
                    "total_trades": row["total_trades"] or 0,
                    "wins": row["wins"] or 0,
                    "losses": row["losses"] or 0,
                    "open_positions": row["open_positions"] or 0,
                    "win_rate": round((row["wins"] / total * 100) if total else 0, 1),
                    "total_pnl": round(float(row["total_pnl"] or 0), 2),
                    "total_volume": round(float(row["total_volume"] or 0), 2),
                    "pnl_24h": round(float(row["pnl_24h"] or 0), 2),
                    "trades_24h": row["trades_24h"] or 0,
                    "best_trade": round(float(row["best_trade"] or 0), 2) if row["best_trade"] else None,
                    "worst_trade": round(float(row["worst_trade"] or 0), 2) if row["worst_trade"] else None,
                }
        except Exception as e:
            print(f"[DB] get_alert_autotrade_stats error: {e}", flush=True)
            return {}


    # ── v8.0: Trade Journal ─────────────────────────────────────────
    async def add_journal_note(self, trade_type: str, trade_id: int, note: str, tags: str = "", user_id: int = 1):
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO trade_journal (trade_type, trade_id, note, tags, user_id) VALUES ($1,$2,$3,$4,$5)",
                trade_type, trade_id, note, tags, user_id
            )

    async def get_journal_notes(self, user_id: int = 1, limit: int = 50):
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM trade_journal WHERE user_id=$1 ORDER BY created_at DESC LIMIT $2",
                user_id, limit
            )
            return [_serialize_row(r) for r in rows]

    # ── v8.0: Bankroll ────────────────────────────────────────────
    async def log_bankroll(self, balance: float, daily_pnl: float, user_id: int = 1):
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO bankroll_history (balance, daily_pnl, user_id) VALUES ($1,$2,$3)",
                balance, daily_pnl, user_id
            )

    async def get_bankroll_history(self, days: int = 30, user_id: int = 1):
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT balance, daily_pnl, created_at FROM bankroll_history WHERE created_at > NOW() - $1::interval AND user_id = $2 ORDER BY created_at",
                f"{days} days", user_id
            )
            return [_serialize_row(r) for r in rows]

    # ── v8.0: Market Making Orders ────────────────────────────────
    async def log_mm_order(self, market_id: str, side: str, price: float, size: float, user_id: int = 1):
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO mm_orders (market_id, side, price, size, user_id) VALUES ($1,$2,$3,$4,$5)",
                market_id, side, price, size, user_id
            )

    async def get_mm_orders(self, limit: int = 50, user_id: int = 1):
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM mm_orders WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2", user_id, limit
            )
            return [_serialize_row(r) for r in rows]

    # ── v8.0: Push Notifications ──────────────────────────────────
    async def create_notification(self, user_id: int, ntype: str, title: str, body: str = ""):
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO push_notifications (user_id, type, title, body) VALUES ($1,$2,$3,$4)",
                user_id, ntype, title, body
            )

    async def get_notifications(self, user_id: int = 1, unread_only: bool = True, limit: int = 20):
        async with self._pool.acquire() as conn:
            if unread_only:
                rows = await conn.fetch(
                    "SELECT * FROM push_notifications WHERE user_id=$1 AND NOT read ORDER BY created_at DESC LIMIT $2",
                    user_id, limit
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM push_notifications WHERE user_id=$1 ORDER BY created_at DESC LIMIT $2",
                    user_id, limit
                )
            return [_serialize_row(r) for r in rows]

    async def mark_notifications_read(self, user_id: int = 1, notification_ids: list = None):
        async with self._pool.acquire() as conn:
            if notification_ids:
                await conn.execute(
                    "UPDATE push_notifications SET read=TRUE WHERE user_id=$1 AND id=ANY($2::int[])",
                    user_id, notification_ids
                )
            else:
                await conn.execute(
                    "UPDATE push_notifications SET read=TRUE WHERE user_id=$1",
                    user_id
                )

    # ── v8.0: Heatmap ────────────────────────────────────────────
    async def get_heatmap_data(self, user_id: int = 1):
        """Volumen y alertas por categoría para heatmap.
        Usa market_category de trades, con fallback a markets_tracked.category
        y a la categoría del slug (crypto-prices, sports, etc.).
        Alertas filtradas por user_id + min_size + min_score."""
        min_size, min_score = await self._get_user_alert_filters(user_id)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                WITH trade_cats AS (
                    SELECT
                        COALESCE(
                            NULLIF(t.market_category, ''),
                            NULLIF(mt.category, ''),
                            CASE
                                WHEN t.market_slug ILIKE '%crypto%' OR t.market_slug ILIKE '%btc%'
                                     OR t.market_slug ILIKE '%eth%' OR t.market_slug ILIKE '%sol%' THEN 'crypto'
                                WHEN t.market_slug ILIKE '%nba%' OR t.market_slug ILIKE '%nfl%'
                                     OR t.market_slug ILIKE '%soccer%' OR t.market_slug ILIKE '%sports%' THEN 'sports'
                                WHEN t.market_slug ILIKE '%president%' OR t.market_slug ILIKE '%election%'
                                     OR t.market_slug ILIKE '%trump%' OR t.market_slug ILIKE '%politic%' THEN 'politics'
                                ELSE 'other'
                            END
                        ) as category,
                        t.size
                    FROM trades t
                    LEFT JOIN markets_tracked mt ON t.market_id = mt.condition_id
                    WHERE t.timestamp > NOW() - INTERVAL '30 days'
                ),
                trade_stats AS (
                    SELECT category, COUNT(*) as total_trades, COALESCE(SUM(size), 0) as total_volume
                    FROM trade_cats
                    GROUP BY category
                ),
                alert_stats AS (
                    SELECT
                        COALESCE(
                            NULLIF(a.market_category, ''),
                            NULLIF(mt.category, ''),
                            'other'
                        ) as category,
                        COUNT(*) as total_alerts,
                        COUNT(*) FILTER (WHERE a.resolved AND a.was_correct) as correct,
                        COUNT(*) FILTER (WHERE a.resolved AND NOT a.was_correct) as incorrect,
                        COUNT(*) FILTER (WHERE a.resolved IS NOT TRUE) as pending,
                        COALESCE(SUM(CASE WHEN a.was_correct THEN a.size ELSE 0 END), 0)
                          - COALESCE(SUM(CASE WHEN a.was_correct = FALSE THEN a.size ELSE 0 END), 0) as total_pnl
                    FROM alerts a
                    LEFT JOIN markets_tracked mt ON a.market_id = mt.condition_id
                    WHERE a.created_at > NOW() - INTERVAL '30 days'
                      AND a.user_id = $1 AND a.size >= $2 AND a.score >= $3
                    GROUP BY COALESCE(NULLIF(a.market_category, ''), NULLIF(mt.category, ''), 'other')
                )
                SELECT
                    COALESCE(t.category, a.category) as category,
                    COALESCE(t.total_trades, 0) as total_trades,
                    COALESCE(a.total_alerts, 0) as total_alerts,
                    COALESCE(a.correct, 0) as correct,
                    COALESCE(a.incorrect, 0) as incorrect,
                    COALESCE(a.pending, 0) as pending,
                    COALESCE(t.total_volume, 0) as total_volume,
                    COALESCE(a.total_pnl, 0) as total_pnl
                FROM trade_stats t
                FULL OUTER JOIN alert_stats a ON t.category = a.category
                ORDER BY COALESCE(t.total_volume, 0) DESC
            """, user_id, min_size, min_score)
            return [dict(r) for r in rows]

    # ── v8.0: ML Training Data ────────────────────────────────────
    async def get_ml_training_data(self):
        """Datos de entrenamiento para ML scoring."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    a.score, a.size, a.triggers, a.was_correct, a.resolved,
                    a.market_category as category, a.created_at,
                    EXTRACT(HOUR FROM a.created_at) as hour,
                    w.total_trades as wallet_trades,
                    CASE WHEN (w.win_count + w.loss_count) > 0
                        THEN (w.win_count::float / (w.win_count + w.loss_count) * 100)
                        ELSE 0 END as wallet_winrate,
                    w.smart_money_score
                FROM alerts a
                LEFT JOIN wallets w ON a.wallet_address = w.address
                WHERE a.resolved = TRUE
                ORDER BY a.created_at DESC
                LIMIT 5000
            """)
            return [dict(r) for r in rows]

    # ── v8.0: Correlation Filter ──────────────────────────────────
    async def get_open_positions_markets(self):
        """Obtener mercados con posiciones abiertas para correlation filter."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT condition_id, market_slug, outcome
                FROM alert_autotrades
                WHERE NOT resolved AND status = 'filled'
            """)
            return [dict(r) for r in rows]

    # ── v8.0: Active Markets ──────────────────────────────────────
    async def get_active_markets(self):
        """Obtener mercados activos trackeados."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM markets_tracked
                WHERE end_date IS NULL OR end_date > NOW()
                ORDER BY updated_at DESC
                LIMIT 100
            """)
            return [dict(r) for r in rows]

    async def get_markets_by_category(self, category: str):
        """Obtener mercados por categoría."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM markets_tracked WHERE category = $1 LIMIT 50",
                category
            )
            return [dict(r) for r in rows]

    # ── v10.0: Category Edge ───────────────────────────────────────

    async def get_category_edge(self) -> list:
        """Win rate por categoría de alertas resueltas."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT
                        COALESCE(NULLIF(market_category, ''), 'other') as category,
                        COUNT(*) FILTER (WHERE resolved) as resolved,
                        COUNT(*) FILTER (WHERE resolved AND was_correct) as wins,
                        CASE WHEN COUNT(*) FILTER (WHERE resolved) > 0
                            THEN ROUND(COUNT(*) FILTER (WHERE resolved AND was_correct)::numeric
                                 / COUNT(*) FILTER (WHERE resolved) * 100, 1)
                            ELSE 0 END as win_rate
                    FROM alerts
                    WHERE created_at > NOW() - INTERVAL '60 days'
                    GROUP BY COALESCE(NULLIF(market_category, ''), 'other')
                    HAVING COUNT(*) FILTER (WHERE resolved) >= 3
                    ORDER BY win_rate DESC
                """)
                return [dict(r) for r in rows]
        except Exception as e:
            return []


def _serialize_row(row) -> dict:
    """Convertir asyncpg Record a dict serializando datetimes."""
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d
