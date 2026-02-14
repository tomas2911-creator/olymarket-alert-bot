"""Telegram notification handler."""
from typing import Optional
import structlog
from telegram import Bot
from telegram.constants import ParseMode

from src.models import AlertCandidate
from src import config

logger = structlog.get_logger()


class TelegramNotifier:
    """Envía alertas a múltiples usuarios de Telegram."""

    def __init__(self):
        self.bot_token = config.TELEGRAM_BOT_TOKEN
        chat_ids_str = config.TELEGRAM_CHAT_IDS
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
        success_count = 0
        for chat_id in self.chat_ids:
            try:
                await self.bot.send_message(
                    chat_id=chat_id, text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=disable_preview,
                )
                success_count += 1
            except Exception as e:
                print(f"Error enviando a {chat_id}: {e}", flush=True)
        return success_count

    async def send_alert(self, candidate: AlertCandidate) -> bool:
        if not self.is_configured:
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
        if not self.is_configured:
            return
        try:
            text = (
                "🤖 <b>Polymarket Alert Bot v2.0 Iniciado</b>\n\n"
                "Monitoreando actividad sospechosa...\n"
                "📊 Dashboard activo en el puerto web"
            )
            await self._send_to_all(text)
        except Exception as e:
            print(f"Error enviando startup: {e}", flush=True)

    async def send_health_check(self, stats: dict):
        if not self.is_configured:
            return
        try:
            hit_rate = stats.get("hit_rate", 0)
            message = (
                f"📊 <b>Health Check</b>\n\n"
                f"• Trades procesados: {stats.get('total_trades_processed', 0):,}\n"
                f"• Alertas totales: {stats.get('total_alerts', 0)}\n"
                f"• Alertas 24h: {stats.get('alerts_last_24h', 0)}\n"
                f"• Hit rate: {hit_rate:.1f}%\n"
                f"• Wallets flaggeadas: {stats.get('unique_wallets_flagged', 0)}\n"
                f"• Score promedio: {stats.get('avg_score', 0)}\n"
                f"• Uptime: {stats.get('uptime', 'N/A')}"
            )
            await self._send_to_all(message)
        except Exception as e:
            print(f"Error enviando health check: {e}", flush=True)

    async def send_resolution_update(self, market_question: str, resolution: str,
                                     correct_count: int, total_count: int):
        """Notificar resolución de mercado."""
        if not self.is_configured:
            return
        try:
            emoji = "✅" if correct_count > 0 else "❌"
            message = (
                f"{emoji} <b>Mercado Resuelto</b>\n\n"
                f"<b>Market:</b> {market_question[:80]}\n"
                f"<b>Resolución:</b> {resolution}\n"
                f"<b>Alertas correctas:</b> {correct_count}/{total_count}"
            )
            await self._send_to_all(message, disable_preview=True)
        except Exception as e:
            print(f"Error enviando resolución: {e}", flush=True)

    async def send_copy_trade_alert(self, trade, candidate: AlertCandidate) -> bool:
        """Enviar alerta especial de copy-trade (smart money)."""
        if not self.is_configured:
            return False
        try:
            wallet_short = f"{trade.wallet_address[:6]}...{trade.wallet_address[-4:]}"
            triggers_text = "\n".join([f"  • {t}" for t in candidate.triggers])
            market_url = f"https://polymarket.com/event/{trade.market_slug}" if trade.market_slug else ""
            poly_profile = f"https://polymarket.com/profile/{trade.wallet_address}"

            message = (
                f"⭐ <b>Smart Money Alert — Copy Trade</b>\n\n"
                f"<b>Mercado:</b> {trade.market_question[:80]}\n"
                f"<b>Apuesta:</b> {trade.outcome} {trade.side} | "
                f"<b>Size:</b> ${trade.size:,.0f} | <b>Precio:</b> {trade.price:.2f}\n\n"
                f"<b>Wallet:</b> <code>{wallet_short}</code>\n"
                f"<b>Score:</b> {candidate.score}/12\n\n"
                f"<b>Señales:</b>\n{triggers_text}\n"
            )
            if candidate.wallet_hit_rate is not None:
                message += f"\n📈 Hit rate: {candidate.wallet_hit_rate:.0f}%"
            if market_url:
                message += f"\n🔗 <a href=\"{market_url}\">Polymarket</a>"
                message += f" | <a href=\"{poly_profile}\">Perfil Trader</a>"
            message += "\n\n💡 <i>Wallet en watchlist por rendimiento histórico.</i>"

            sent = await self._send_to_all(message, disable_preview=True)
            return sent > 0
        except Exception as e:
            print(f"Error enviando copy-trade alert: {e}", flush=True)
            return False

    async def send_crypto_signal(self, signal: dict) -> bool:
        """Enviar señal de crypto arb a Telegram."""
        if not self.is_configured:
            return False
        try:
            coin = signal.get("coin", "?")
            direction = signal.get("direction", "?").upper()
            emoji = "🟢" if direction == "UP" else "🔴"
            conf = float(signal.get("confidence", 0) or 0)
            edge = float(signal.get("edge_pct", 0) or 0)
            poly_odds = float(signal.get("poly_odds", 0) or 0)
            fair_odds = float(signal.get("fair_odds", 0) or 0)
            spot = float(signal.get("spot_price", 0) or 0)
            change = float(signal.get("spot_change_pct", 0) or 0)
            time_rem = int(signal.get("time_remaining_sec", 0) or 0)
            minutes = time_rem // 60
            seconds = time_rem % 60
            expected = float(signal.get("expected_profit_pct", 0) or 0)

            # Barra de confianza
            filled = int(conf / 10)
            conf_bar = "█" * filled + "░" * (10 - filled)

            message = (
                f"{emoji} <b>Crypto Arb Signal — {coin} {direction}</b>\n\n"
                f"<b>Spot:</b> ${spot:,.2f} ({change:+.3f}%)\n"
                f"<b>Polymarket:</b> {poly_odds:.2f} → Fair: {fair_odds:.2f}\n"
                f"<b>Edge:</b> {edge:.1f}% | <b>Profit est:</b> {expected:.0f}%\n\n"
                f"<b>Confianza:</b> [{conf_bar}] {conf:.0f}%\n"
                f"<b>Cierra en:</b> {minutes}m {seconds}s\n\n"
                f"💡 <i>Señal automática. Paper trading.</i>"
            )
            sent = await self._send_to_all(message, disable_preview=True)
            return sent > 0
        except Exception as e:
            print(f"Error enviando crypto signal: {e}", flush=True)
            return False

    def _format_message(self, candidate: AlertCandidate) -> str:
        trade = candidate.trade
        wallet_short = f"{trade.wallet_address[:6]}...{trade.wallet_address[-4:]}"
        triggers_text = "\n".join([f"  • {t}" for t in candidate.triggers])
        market_url = f"https://polymarket.com/event/{trade.market_slug}" if trade.market_slug else ""
        wallet_trades = candidate.wallet_stats.total_trades if candidate.wallet_stats else 0

        # Score bar visual
        filled = min(candidate.score, 12)
        score_bar = "█" * filled + "░" * (12 - filled)

        message = (
            f"🚨 <b>Actividad Sospechosa Detectada</b>\n\n"
            f"<b>Mercado:</b> {trade.market_question[:80]}{'...' if len(trade.market_question) > 80 else ''}\n"
            f"<b>Apuesta:</b> {trade.outcome} {trade.side} | "
            f"<b>Size:</b> ${trade.size:,.0f} | <b>Precio:</b> {trade.price:.2f}\n\n"
            f"<b>Wallet:</b> <code>{wallet_short}</code> ({wallet_trades} trades)\n"
            f"<b>Score:</b> [{score_bar}] {candidate.score}/12\n\n"
            f"<b>Señales:</b>\n{triggers_text}\n"
        )

        # Info extra
        if candidate.days_to_resolution is not None and candidate.days_to_resolution < 30:
            message += f"\n📅 Cierra en {candidate.days_to_resolution:.1f} días"

        if candidate.wallet_hit_rate is not None:
            message += f"\n📈 Hit rate wallet: {candidate.wallet_hit_rate:.0f}%"

        if candidate.cluster_wallets and len(candidate.cluster_wallets) >= 2:
            message += f"\n👥 {len(candidate.cluster_wallets)} wallets en cluster"

        message += "\n"

        if market_url:
            message += f"\n🔗 <a href=\"{market_url}\">Ver en Polymarket</a>\n"

        message += "\n⚠️ <i>Alerta de anomalía. DYOR.</i>"
        return message
