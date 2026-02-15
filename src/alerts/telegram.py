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
                "🤖 <b>Polymarket Alert Bot v3.0 Iniciado</b>\n\n"
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

            max_score = self._calc_max_score()
            bar_len = 12
            filled = min(int(candidate.score / max(max_score, 1) * bar_len), bar_len)
            score_bar = "█" * filled + "░" * (bar_len - filled)

            message = (
                f"⭐ <b>Smart Money Alert — Copy Trade</b>\n\n"
                f"<b>Mercado:</b> {trade.market_question[:80]}\n"
                f"<b>Apuesta:</b> {trade.outcome} {trade.side} | "
                f"<b>Size:</b> ${trade.size:,.0f} | <b>Precio:</b> {trade.price:.2f}\n\n"
                f"<b>Wallet:</b> <code>{wallet_short}</code>\n"
                f"<b>Score:</b> [{score_bar}] {candidate.score}/{max_score}\n\n"
                f"<b>Señales:</b>\n{triggers_text}\n"
            )
            if candidate.wallet_hit_rate is not None:
                message += f"\n📈 Hit rate: {candidate.wallet_hit_rate:.0f}%"
            if market_url:
                message += f'\n🔗 <a href="{market_url}">Polymarket</a>'
                message += f' | <a href="{poly_profile}">Perfil Trader</a>'
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

    async def send_kill_switch_alert(self, action: str, reason: str = ""):
        """Enviar notificación de kill switch activado/desactivado."""
        if not self.is_configured:
            return
        try:
            emoji = "🛑" if action == "stop" else "✅"
            label = "DETENIDO" if action == "stop" else "REANUDADO"
            message = (
                f"{emoji} <b>Kill Switch — Trading {label}</b>\n\n"
                f"<b>Acción:</b> {action}\n"
            )
            if reason:
                message += f"<b>Razón:</b> {reason}\n"
            message += "\n💡 <i>Usa /start o /stop para controlar el bot remotamente.</i>"
            await self._send_to_all(message)
        except Exception as e:
            print(f"Error enviando kill switch alert: {e}", flush=True)

    async def send_trade_notification(self, trade_info: dict, action: str = "buy"):
        """Enviar notificación de trade ejecutado."""
        if not self.is_configured:
            return
        try:
            emoji = "🟢" if action == "buy" else "🔴"
            market = trade_info.get("market_slug", "")[:40]
            outcome = trade_info.get("outcome", "")
            size = trade_info.get("size_usd", 0)
            price = trade_info.get("price", 0)
            score = trade_info.get("alert_score", 0)
            message = (
                f"{emoji} <b>Trade {'Ejecutado' if action == 'buy' else 'Cerrado'}</b>\n\n"
                f"<b>Mercado:</b> {market}\n"
                f"<b>Outcome:</b> {outcome} | <b>${size:.2f}</b> @ {price:.2f}\n"
                f"<b>Score:</b> {score}\n"
            )
            if action != "buy":
                pnl = trade_info.get("pnl", 0)
                result = trade_info.get("result", "")
                emoji_r = "✅" if pnl >= 0 else "❌"
                message += f"\n{emoji_r} <b>PnL:</b> ${pnl:.2f} ({result})"
            await self._send_to_all(message, disable_preview=True)
        except Exception as e:
            print(f"Error enviando trade notification: {e}", flush=True)

    async def check_commands(self, app_state=None) -> Optional[str]:
        """Verificar si hay comandos de Telegram (/stop, /start, /status).
        Retorna el comando recibido o None.
        """
        if not self.is_configured:
            return None
        try:
            updates = await self.bot.get_updates(timeout=1, limit=5)
            for update in updates:
                if update.message and update.message.text:
                    text = update.message.text.strip().lower()
                    chat_id = str(update.message.chat_id)
                    # Solo aceptar de chat_ids autorizados
                    if chat_id not in self.chat_ids:
                        continue
                    if text == "/stop":
                        return "stop"
                    elif text == "/start":
                        return "start"
                    elif text == "/status":
                        return "status"
        except Exception:
            pass
        return None

    def _calc_max_score(self) -> int:
        """Calcular máximo score posible sumando todas las señales configuradas."""
        return (
            config.FRESH_WALLET_POINTS + config.LARGE_SIZE_POINTS +
            config.MARKET_ANOMALY_POINTS + config.WALLET_SHIFT_POINTS +
            config.CONCENTRATION_POINTS + config.TIME_PROXIMITY_POINTS +
            config.CLUSTER_POINTS + config.HIT_RATE_POINTS +
            config.CONTRARIAN_POINTS + config.ACCUMULATION_POINTS +
            config.PROVEN_WINNER_POINTS + config.MULTI_SMART_POINTS +
            config.LATE_INSIDER_POINTS + config.EXIT_ALERT_POINTS +
            # Señales opcionales (módulos)
            (config.ORDERBOOK_DEPTH_POINTS if config.FEATURE_ORDERBOOK_DEPTH else 0) +
            (config.NICHE_MARKET_POINTS if config.FEATURE_MARKET_CLASSIFICATION else 0) +
            (config.BASKET_POINTS + config.CROSS_BASKET_EXTRA_POINTS if config.FEATURE_WALLET_BASKETS else 0) +
            (config.SNIPER_POINTS if config.FEATURE_SNIPER_DBSCAN else 0) +
            # Señales v7.0
            (config.CONSENSUS_SHIFT_POINTS if config.FEATURE_CONSENSUS_SHIFT else 0) +
            (config.RESOLUTION_PATTERN_POINTS if config.FEATURE_RESOLUTION_PATTERN else 0) +
            config.WHALE_ALERT_POINTS +
            config.REPEAT_WINNER_POINTS
        )

    def _format_message(self, candidate: AlertCandidate) -> str:
        trade = candidate.trade
        wallet_short = f"{trade.wallet_address[:6]}...{trade.wallet_address[-4:]}"
        triggers_text = "\n".join([f"  • {t}" for t in candidate.triggers])
        market_url = f"https://polymarket.com/event/{trade.market_slug}" if trade.market_slug else ""
        wallet_trades = candidate.wallet_stats.total_trades if candidate.wallet_stats else 0

        # Score bar visual — máximo dinámico basado en señales configuradas
        max_score = self._calc_max_score()
        bar_len = 12
        filled = min(int(candidate.score / max(max_score, 1) * bar_len), bar_len)
        score_bar = "█" * filled + "░" * (bar_len - filled)

        message = (
            f"🚨 <b>Actividad Sospechosa Detectada</b>\n\n"
            f"<b>Mercado:</b> {trade.market_question[:80]}{'...' if len(trade.market_question) > 80 else ''}\n"
            f"<b>Apuesta:</b> {trade.outcome} {trade.side} | "
            f"<b>Size:</b> ${trade.size:,.0f} | <b>Precio:</b> {trade.price:.2f}\n\n"
            f"<b>Wallet:</b> <code>{wallet_short}</code> ({wallet_trades} trades)\n"
            f"<b>Score:</b> [{score_bar}] {candidate.score}/{max_score}\n\n"
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
