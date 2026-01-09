"""SQLite database for storing wallet stats, market baselines, and alert history."""
import aiosqlite
from datetime import datetime, timedelta
from typing import Optional
import structlog

from src.models import WalletStats, MarketBaseline, Trade

logger = structlog.get_logger()

DB_PATH = "polymarket_alerts.db"


class Database:
    """Async SQLite database handler."""
    
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
    
    async def connect(self):
        """Connect to database and create tables."""
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._create_tables()
        logger.info("database_connected", path=self.db_path)
    
    async def close(self):
        """Close database connection."""
        if self._conn:
            await self._conn.close()
            logger.info("database_closed")
    
    async def _create_tables(self):
        """Create database tables if they don't exist."""
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS wallets (
                address TEXT PRIMARY KEY,
                total_trades INTEGER DEFAULT 0,
                first_seen TIMESTAMP,
                last_seen TIMESTAMP,
                total_volume REAL DEFAULT 0,
                avg_trade_size REAL DEFAULT 0
            );
            
            CREATE TABLE IF NOT EXISTS trades (
                transaction_hash TEXT PRIMARY KEY,
                market_id TEXT,
                wallet_address TEXT,
                side TEXT,
                size REAL,
                price REAL,
                timestamp TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS market_baselines (
                condition_id TEXT PRIMARY KEY,
                trade_count INTEGER DEFAULT 0,
                total_volume REAL DEFAULT 0,
                avg_trade_size REAL DEFAULT 0,
                median_trade_size REAL DEFAULT 0,
                p90_trade_size REAL DEFAULT 0,
                p95_trade_size REAL DEFAULT 0,
                last_updated TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_address TEXT,
                market_id TEXT,
                side TEXT,
                score INTEGER,
                triggers TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades(wallet_address);
            CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
            CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_alerts_wallet_market ON alerts(wallet_address, market_id);
        """)
        await self._conn.commit()
    
    async def get_wallet_stats(self, address: str) -> Optional[WalletStats]:
        """Get statistics for a wallet."""
        async with self._conn.execute(
            "SELECT * FROM wallets WHERE address = ?",
            (address.lower(),)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return WalletStats(
                    address=row["address"],
                    total_trades=row["total_trades"],
                    first_seen=datetime.fromisoformat(row["first_seen"]) if row["first_seen"] else datetime.now(),
                    last_seen=datetime.fromisoformat(row["last_seen"]) if row["last_seen"] else datetime.now(),
                    total_volume=row["total_volume"] or 0,
                    avg_trade_size=row["avg_trade_size"] or 0
                )
        return None
    
    async def update_wallet_stats(self, trade: Trade):
        """Update wallet statistics with a new trade."""
        address = trade.wallet_address.lower()
        now = datetime.now().isoformat()
        
        # Get existing stats
        stats = await self.get_wallet_stats(address)
        
        if stats:
            new_total_trades = stats.total_trades + 1
            new_total_volume = stats.total_volume + trade.size
            new_avg = new_total_volume / new_total_trades
            
            await self._conn.execute("""
                UPDATE wallets 
                SET total_trades = ?, last_seen = ?, total_volume = ?, avg_trade_size = ?
                WHERE address = ?
            """, (new_total_trades, now, new_total_volume, new_avg, address))
        else:
            await self._conn.execute("""
                INSERT INTO wallets (address, total_trades, first_seen, last_seen, total_volume, avg_trade_size)
                VALUES (?, 1, ?, ?, ?, ?)
            """, (address, now, now, trade.size, trade.size))
        
        await self._conn.commit()
    
    async def record_trade(self, trade: Trade) -> bool:
        """Record a trade. Returns False if already exists."""
        try:
            await self._conn.execute("""
                INSERT OR IGNORE INTO trades 
                (transaction_hash, market_id, wallet_address, side, size, price, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.transaction_hash,
                trade.market_id,
                trade.wallet_address.lower(),
                trade.side,
                trade.size,
                trade.price,
                trade.timestamp.isoformat()
            ))
            await self._conn.commit()
            return self._conn.total_changes > 0
        except Exception as e:
            logger.warning("failed_to_record_trade", error=str(e))
            return False
    
    async def is_trade_processed(self, transaction_hash: str) -> bool:
        """Check if a trade has already been processed."""
        async with self._conn.execute(
            "SELECT 1 FROM trades WHERE transaction_hash = ?",
            (transaction_hash,)
        ) as cursor:
            return await cursor.fetchone() is not None
    
    async def get_market_baseline(self, condition_id: str) -> Optional[MarketBaseline]:
        """Get baseline statistics for a market."""
        async with self._conn.execute(
            "SELECT * FROM market_baselines WHERE condition_id = ?",
            (condition_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return MarketBaseline(
                    condition_id=row["condition_id"],
                    trade_count=row["trade_count"],
                    total_volume=row["total_volume"],
                    avg_trade_size=row["avg_trade_size"],
                    median_trade_size=row["median_trade_size"],
                    p90_trade_size=row["p90_trade_size"],
                    p95_trade_size=row["p95_trade_size"],
                    last_updated=datetime.fromisoformat(row["last_updated"]) if row["last_updated"] else datetime.now()
                )
        return None
    
    async def update_market_baseline(self, condition_id: str):
        """Recalculate and update market baseline from stored trades."""
        async with self._conn.execute("""
            SELECT size FROM trades 
            WHERE market_id = ? 
            ORDER BY size
        """, (condition_id,)) as cursor:
            rows = await cursor.fetchall()
        
        if not rows:
            return
        
        sizes = [row["size"] for row in rows]
        n = len(sizes)
        
        total = sum(sizes)
        avg = total / n
        median = sizes[n // 2]
        p90 = sizes[int(n * 0.90)] if n > 10 else sizes[-1]
        p95 = sizes[int(n * 0.95)] if n > 20 else sizes[-1]
        
        await self._conn.execute("""
            INSERT OR REPLACE INTO market_baselines 
            (condition_id, trade_count, total_volume, avg_trade_size, median_trade_size, p90_trade_size, p95_trade_size, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (condition_id, n, total, avg, median, p90, p95, datetime.now().isoformat()))
        await self._conn.commit()
    
    async def should_alert(self, wallet: str, market_id: str, cooldown_hours: int = 6) -> bool:
        """Check if we should send an alert (respecting cooldown)."""
        cutoff = (datetime.now() - timedelta(hours=cooldown_hours)).isoformat()
        
        async with self._conn.execute("""
            SELECT 1 FROM alerts 
            WHERE wallet_address = ? AND market_id = ? AND created_at > ?
        """, (wallet.lower(), market_id, cutoff)) as cursor:
            return await cursor.fetchone() is None
    
    async def record_alert(self, wallet: str, market_id: str, side: str, score: int, triggers: list[str]):
        """Record that an alert was sent."""
        await self._conn.execute("""
            INSERT INTO alerts (wallet_address, market_id, side, score, triggers)
            VALUES (?, ?, ?, ?, ?)
        """, (wallet.lower(), market_id, side, score, ",".join(triggers)))
        await self._conn.commit()
