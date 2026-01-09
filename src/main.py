"""Main entry point for Polymarket Alert Bot."""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml
import structlog
from dotenv import load_dotenv

from src.api.polymarket import PolymarketClient
from src.storage.database import Database
from src.detection.analyzer import AnomalyAnalyzer
from src.alerts.telegram import TelegramNotifier

# Load environment variables
load_dotenv()

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.dev.ConsoleRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


def load_config() -> dict:
    """Load configuration from YAML file."""
    config_path = Path(__file__).parent.parent / "config.yaml"
    
    if not config_path.exists():
        logger.warning("config_file_not_found", path=str(config_path))
        return {}
    
    with open(config_path) as f:
        config = yaml.safe_load(f)
    
    logger.info("config_loaded", path=str(config_path))
    return config


class PolymarketAlertBot:
    """Main bot class that orchestrates all components."""
    
    def __init__(self, config: dict):
        self.config = config
        self.db = Database()
        self.analyzer = AnomalyAnalyzer(config)
        self.notifier = TelegramNotifier()
        
        # Polling settings
        polling_config = config.get("polling", {})
        self.poll_interval = polling_config.get("interval_seconds", 60)
        self.max_markets = polling_config.get("max_markets", 50)
        
        # Alert settings
        alert_config = config.get("alerts", {})
        self.cooldown_hours = alert_config.get("cooldown_hours", 6)
        
        # Stats
        self.start_time = datetime.now()
        self.trades_processed = 0
        self.alerts_sent = 0
    
    async def start(self):
        """Start the bot."""
        logger.info("bot_starting")
        
        # Connect to database
        await self.db.connect()
        
        # Send startup message
        await self.notifier.send_startup_message()
        
        logger.info("bot_started", poll_interval=self.poll_interval)
        
        # Main loop
        while True:
            try:
                await self.poll_cycle()
            except Exception as e:
                logger.error("poll_cycle_error", error=str(e))
            
            await asyncio.sleep(self.poll_interval)
    
    async def poll_cycle(self):
        """Execute one polling cycle."""
        logger.info("poll_cycle_start")
        
        async with PolymarketClient() as client:
            # 1. Fetch active markets
            markets = await client.get_markets(limit=self.max_markets)
            
            if not markets:
                logger.warning("no_markets_found")
                return
            
            # 2. Fetch recent trades
            trades = await client.get_all_recent_trades(markets, trades_per_market=20)
            
            logger.info("trades_fetched", count=len(trades))
            
            # 3. Process each trade
            for trade in trades:
                await self.process_trade(trade)
        
        logger.info("poll_cycle_complete", processed=self.trades_processed, alerts=self.alerts_sent)
    
    async def process_trade(self, trade):
        """Process a single trade for anomalies."""
        # Skip if already processed
        if await self.db.is_trade_processed(trade.transaction_hash):
            return
        
        # Skip small trades early
        min_size = self.config.get("detection", {}).get("min_size_usd", 2000)
        if trade.size < min_size:
            await self.db.record_trade(trade)
            await self.db.update_wallet_stats(trade)
            return
        
        # Get wallet stats
        wallet_stats = await self.db.get_wallet_stats(trade.wallet_address)
        
        # Get market baseline
        market_baseline = await self.db.get_market_baseline(trade.market_id)
        
        # Analyze trade
        candidate = self.analyzer.analyze(trade, wallet_stats, market_baseline)
        
        self.trades_processed += 1
        
        # Check if should alert
        if self.analyzer.should_alert(candidate):
            # Check cooldown
            if await self.db.should_alert(trade.wallet_address, trade.market_id, self.cooldown_hours):
                # Send alert
                success = await self.notifier.send_alert(candidate)
                
                if success:
                    self.alerts_sent += 1
                    await self.db.record_alert(
                        trade.wallet_address,
                        trade.market_id,
                        trade.side,
                        candidate.score,
                        candidate.triggers
                    )
                    logger.info(
                        "alert_triggered",
                        wallet=trade.wallet_address[:10],
                        market=trade.market_slug,
                        score=candidate.score,
                        triggers=candidate.triggers
                    )
            else:
                logger.debug("alert_cooldown", wallet=trade.wallet_address[:10], market=trade.market_slug)
        
        # Record trade and update stats
        await self.db.record_trade(trade)
        await self.db.update_wallet_stats(trade)
        
        # Periodically update market baseline
        if self.trades_processed % 100 == 0:
            await self.db.update_market_baseline(trade.market_id)
    
    async def shutdown(self):
        """Graceful shutdown."""
        logger.info("bot_shutting_down")
        await self.db.close()


async def main():
    """Main entry point."""
    config = load_config()
    bot = PolymarketAlertBot(config)
    
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt")
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
