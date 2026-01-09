# Polymarket Insider-Move Alert Bot

Detects abnormal wallet activity on Polymarket prediction markets and sends alerts to Telegram.

## Features

- 🔍 **Anomaly Detection**: Identifies suspicious trading patterns
- 💰 **Size Anomalies**: Flags unusually large trades
- 🆕 **Fresh Wallet Detection**: Tracks new wallets making big bets
- 📊 **Behavior Shift**: Detects when wallets deviate from their normal patterns
- 📱 **Telegram Alerts**: Real-time notifications with context

## Scoring System

| Signal | Points | Description |
|--------|--------|-------------|
| Fresh wallet | 2 | Wallet with ≤5 trades |
| Large absolute size | 2 | Trade ≥ $2,000 |
| Market size anomaly | 2 | Trade ≥ 95th percentile for market |
| Wallet behavior shift | 3 | Trade ≥ 5x wallet's average |
| High concentration | 3 | Trade ≥ 10% of market volume |

Alert triggers when **score ≥ 5**.

## Setup

### 1. Clone and Install

```bash
git clone https://github.com/yourusername/polymarket-alert-bot.git
cd polymarket-alert-bot
pip install -r requirements.txt
```

### 2. Create Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow instructions
3. Copy the bot token

### 3. Get Chat ID

1. Message your new bot
2. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Find your `chat_id` in the response

### 4. Configure Environment

```bash
cp .env.example .env
```

Edit `.env`:
```
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

### 5. Run Locally

```bash
python -m src.main
```

## Deploy to Railway

1. Push to GitHub
2. Create new project in [Railway](https://railway.app)
3. Connect your GitHub repo
4. Add environment variables:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
5. Deploy!

## Configuration

Edit `config.yaml` to adjust:

```yaml
polling:
  interval_seconds: 60  # How often to check

detection:
  min_size_usd: 2000    # Minimum trade size
  fresh_wallet_max_trades: 5

scoring:
  alert_threshold: 5    # Minimum score to alert
```

## Alert Format

```
🚨 Potential Insider-Like Activity

Market: Will X happen by Y date?
Side: YES | Size: $5,000 | Price: 0.65

Wallet: 0x1234...5678 (2 trades)
Score: 7/10

Triggers:
  • 🆕 Fresh wallet (2 trades)
  • 💰 Large size ($5,000)
  • 🔄 Behavior shift (8.3x avg)

🔗 View Market

⚠️ Anomaly alert only. DYOR.
```

## License

MIT
