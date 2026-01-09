"""Telegram notification handler."""
import os
from typing import Optional
import structlog
from telegram import Bot
from telegram.constants import ParseMode

from src.models import AlertCandidate

logger = structlog.get_logger()


class TelegramNotifier:
    """Sends alerts to Telegram."""
    
    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self._bot: Optional[Bot] = None
        
        if not self.bot_token:
            logger.warning("telegram_bot_token_missing")
        if not self.chat_id:
            logger.warning("telegram_chat_id_missing")
    
    @property
    def bot(self) -> Bot:
        if self._bot is None:
            self._bot = Bot(token=self.bot_token)
        return self._bot
    
    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)
    
    async def send_alert(self, candidate: AlertCandidate) -> bool:
        """Send an alert for a suspicious trade."""
        if not self.is_configured:
            logger.warning("telegram_not_configured")
            return False
        
        try:
            message = self._format_message(candidate)
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            logger.info(
                "alert_sent",
                wallet=candidate.trade.wallet_address[:10],
                market=candidate.trade.market_slug,
                score=candidate.score
            )
            return True
        except Exception as e:
            logger.error("failed_to_send_alert", error=str(e))
            return False
    
    async def send_startup_message(self):
        """Send a startup notification."""
        if not self.is_configured:
            return
        
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text="🤖 <b>Polymarket Alert Bot Started</b>\n\nMonitoring for suspicious activity...",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error("failed_to_send_startup", error=str(e))
    
    async def send_health_check(self, stats: dict):
        """Send periodic health check message."""
        if not self.is_configured:
            return
        
        try:
            message = (
                f"📊 <b>Health Check</b>\n\n"
                f"• Markets monitored: {stats.get('markets', 0)}\n"
                f"• Trades processed: {stats.get('trades', 0)}\n"
                f"• Alerts sent: {stats.get('alerts', 0)}\n"
                f"• Uptime: {stats.get('uptime', 'N/A')}"
            )
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error("failed_to_send_health_check", error=str(e))
    
    def _format_message(self, candidate: AlertCandidate) -> str:
        """Format alert message for Telegram."""
        trade = candidate.trade
        wallet_short = f"{trade.wallet_address[:6]}...{trade.wallet_address[-4:]}"
        
        # Build triggers list
        triggers_text = "\n".join([f"  • {t}" for t in candidate.triggers])
        
        # Market URL
        market_url = f"https://polymarket.com/event/{trade.market_slug}" if trade.market_slug else ""
        
        # Wallet trades count
        wallet_trades = candidate.wallet_stats.total_trades if candidate.wallet_stats else 0
        
        message = (
            f"🚨 <b>Potential Insider-Like Activity</b>\n\n"
            f"<b>Market:</b> {trade.market_question[:80]}{'...' if len(trade.market_question) > 80 else ''}\n"
            f"<b>Side:</b> {trade.side} | <b>Size:</b> ${trade.size:,.2f} | <b>Price:</b> {trade.price:.2f}\n\n"
            f"<b>Wallet:</b> <code>{wallet_short}</code> ({wallet_trades} trades)\n"
            f"<b>Score:</b> {candidate.score}/10\n\n"
            f"<b>Triggers:</b>\n{triggers_text}\n\n"
        )
        
        if market_url:
            message += f"🔗 <a href=\"{market_url}\">View Market</a>\n\n"
        
        message += "⚠️ <i>Anomaly alert only. DYOR.</i>"
        
        return message
