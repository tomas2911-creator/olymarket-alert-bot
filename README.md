# Polymarket Insider-Move Alert Bot v2.0

Detecta actividad anómala de wallets en Polymarket y envía alertas a Telegram + dashboard web en tiempo real.

## Features

- 🔍 **Detección de anomalías** — patrones sospechosos de trading
- 💰 **Trades grandes** — montos inusualmente altos
- 🆕 **Wallets nuevas** — wallets con pocas transacciones haciendo apuestas grandes
- � **Cambio de comportamiento** — wallets que se desvían de su patrón normal
- ⏰ **Proximidad al cierre** — trades grandes cerca de la resolución del mercado
- 👥 **Clustering** — múltiples wallets apostando al mismo lado simultáneamente
- 🏆 **Hit rate tracking** — seguimiento de aciertos de wallets alertadas
- 📱 **Alertas Telegram** — notificaciones en tiempo real con contexto
- 🌐 **Dashboard web** — visualización completa de alertas, wallets y métricas
- 🗄️ **PostgreSQL** — datos persistentes entre redeploys

## Sistema de Scoring

| Señal | Puntos | Descripción |
|-------|--------|-------------|
| Wallet nueva | 2 | ≤5 trades o <30 días |
| Trade grande | 2 | ≥ $5,000 |
| Anomalía de mercado | 2 | ≥ percentil 95 del mercado |
| Cambio de comportamiento | 3 | ≥ 5x promedio de la wallet |
| Alta concentración | 3 | ≥ 10% del volumen del mercado |
| Proximidad al cierre | 3 | Mercado cierra en ≤7 días |
| Cluster de wallets | 3 | ≥3 wallets mismo lado en 30min |
| Hit rate alto | 2 | Wallet con ≥70% de aciertos |

Alerta se dispara cuando **score ≥ 5**.

## Setup

### Variables de entorno

```
TELEGRAM_BOT_TOKEN=tu_token_de_bot
TELEGRAM_CHAT_IDS=chat_id_1,chat_id_2
DATABASE_URL=postgresql://user:pass@host:port/dbname
```

### Deploy en Railway

1. Push a GitHub
2. Crear proyecto en [Railway](https://railway.app)
3. Agregar addon PostgreSQL
4. Conectar repo de GitHub
5. Agregar variables de entorno (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`)
6. Railway provee `DATABASE_URL` automáticamente
7. Deploy!

El dashboard estará disponible en la URL que Railway asigne.

### Local

```bash
pip install -r requirements.txt
cp .env.example .env
# Editar .env con tus valores
uvicorn src.main:app --host 0.0.0.0 --port 8080
```

## Dashboard

El dashboard web muestra:
- **Stats generales** — alertas totales, hit rate, wallets flaggeadas
- **Alertas recientes** — tabla con mercado, apuesta, score, estado
- **Top wallets** — wallets con más alertas y su hit rate
- **Charts** — alertas por día, distribución de scores

## License

MIT
