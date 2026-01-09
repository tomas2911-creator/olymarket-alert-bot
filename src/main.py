"""Entry point principal del Polymarket Alert Bot."""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Forzar output sin buffer
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

print("=== POLYMARKET ALERT BOT INICIANDO ===", flush=True)

import yaml
import structlog
from dotenv import load_dotenv

print("Imports básicos OK", flush=True)

try:
    from src.api.polymarket import PolymarketClient
    print("Import PolymarketClient OK", flush=True)
    from src.storage.database import Database
    print("Import Database OK", flush=True)
    from src.detection.analyzer import AnomalyAnalyzer
    print("Import AnomalyAnalyzer OK", flush=True)
    from src.alerts.telegram import TelegramNotifier
    print("Import TelegramNotifier OK", flush=True)
except Exception as e:
    print(f"ERROR en imports: {e}", flush=True)
    raise

# Load environment variables
load_dotenv()
print("dotenv cargado OK", flush=True)

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
        """Ejecutar un ciclo de polling."""
        logger.info("ciclo_polling_inicio")
        
        async with PolymarketClient() as client:
            # Obtener trades recientes de toda la plataforma (endpoint público)
            trades = await client.get_recent_trades(limit=200)
            
            if not trades:
                logger.warning("no_trades_encontrados")
                return
            
            logger.info("trades_obtenidos", count=len(trades))
            
            # Procesar cada trade
            for trade in trades:
                await self.process_trade(trade)
        
        logger.info("ciclo_polling_completo", procesados=self.trades_processed, alertas=self.alerts_sent)
    
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
    print("=== INICIANDO MAIN ===", flush=True)
    try:
        config = load_config()
        print(f"Config cargada: {bool(config)}", flush=True)
        bot = PolymarketAlertBot(config)
        print("Bot creado OK", flush=True)
        await bot.start()
    except KeyboardInterrupt:
        print("Keyboard interrupt", flush=True)
    except Exception as e:
        print(f"ERROR en main: {e}", flush=True)
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    print("=== EJECUTANDO ASYNCIO.RUN ===", flush=True)
    asyncio.run(main())
