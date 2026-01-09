"""Telegram notification handler."""
import os
from typing import Optional
import structlog
from telegram import Bot
from telegram.constants import ParseMode

from src.models import AlertCandidate

logger = structlog.get_logger()


class TelegramNotifier:
    """Envía alertas a múltiples usuarios de Telegram."""
    
    def __init__(self, bot_token: Optional[str] = None, chat_ids: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        chat_ids_str = chat_ids or os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_ID")
        
        # Soporta múltiples IDs separados por coma: "123,456,789"
        self.chat_ids: list[str] = []
        if chat_ids_str:
            self.chat_ids = [cid.strip() for cid in chat_ids_str.split(",") if cid.strip()]
        
        self._bot: Optional[Bot] = None
        
        if not self.bot_token:
            print("WARNING: TELEGRAM_BOT_TOKEN no configurado", flush=True)
        if not self.chat_ids:
            print("WARNING: TELEGRAM_CHAT_IDS no configurado", flush=True)
        else:
            print(f"Telegram configurado para {len(self.chat_ids)} usuario(s)", flush=True)
    
    @property
    def bot(self) -> Bot:
        if self._bot is None:
            self._bot = Bot(token=self.bot_token)
        return self._bot
    
    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_ids)
    
    async def _send_to_all(self, text: str, disable_preview: bool = False) -> int:
        """Envía mensaje a todos los chat IDs configurados. Retorna cantidad de envíos exitosos."""
        success_count = 0
        for chat_id in self.chat_ids:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=disable_preview
                )
                success_count += 1
            except Exception as e:
                print(f"Error enviando a {chat_id}: {e}", flush=True)
        return success_count
    
    async def send_alert(self, candidate: AlertCandidate) -> bool:
        """Envía alerta a todos los usuarios."""
        if not self.is_configured:
            print("Telegram no configurado", flush=True)
            return False
        
        try:
            message = self._format_message(candidate)
            sent = await self._send_to_all(message, disable_preview=True)
            print(f"Alerta enviada a {sent}/{len(self.chat_ids)} usuarios", flush=True)
            return sent > 0
        except Exception as e:
            print(f"Error enviando alerta: {e}", flush=True)
            return False
    
    async def send_startup_message(self):
        """Envía mensaje de inicio a todos los usuarios."""
        if not self.is_configured:
            return
        
        try:
            text = "🤖 <b>Polymarket Alert Bot Started</b>\n\nMonitoring for suspicious activity..."
            sent = await self._send_to_all(text)
            print(f"Mensaje startup enviado a {sent} usuario(s)", flush=True)
        except Exception as e:
            print(f"Error enviando startup: {e}", flush=True)
    
    async def send_health_check(self, stats: dict):
        """Envía health check a todos los usuarios."""
        if not self.is_configured:
            return
        
        try:
            message = (
                f"📊 <b>Health Check</b>\n\n"
                f"• Trades procesados: {stats.get('trades', 0)}\n"
                f"• Alertas enviadas: {stats.get('alerts', 0)}\n"
                f"• Uptime: {stats.get('uptime', 'N/A')}"
            )
            await self._send_to_all(message)
        except Exception as e:
            print(f"Error enviando health check: {e}", flush=True)
    
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
