# Polymarket Insider Alert Bot — Manual de Usuario

**Versión:** 6.0  
**Última actualización:** Febrero 2026

---

## Tabla de Contenidos

1. [Resumen General](#1-resumen-general)
2. [Cómo Funciona el Bot](#2-cómo-funciona-el-bot)
3. [Dashboard Web](#3-dashboard-web)
4. [Sistema de Scoring — Las 18 Señales](#4-sistema-de-scoring--las-18-señales)
5. [Configuración General](#5-configuración-general)
6. [Módulos Opcionales](#6-módulos-opcionales)
7. [Crypto Arb — Bot de Arbitraje Crypto](#7-crypto-arb--bot-de-arbitraje-crypto)
8. [Alertas de Telegram](#8-alertas-de-telegram)
9. [Smart Money y Watchlist](#9-smart-money-y-watchlist)
10. [Categorías Excluidas](#10-categorías-excluidas)
11. [Guía de Configuración Recomendada](#11-guía-de-configuración-recomendada)
12. [Preguntas Frecuentes](#12-preguntas-frecuentes)

---

## 1. Resumen General

### ¿Qué es este bot?

El **Polymarket Insider Alert Bot** es un sistema automatizado que monitorea en tiempo real todos los trades que ocurren en [Polymarket](https://polymarket.com) (mercado de predicciones descentralizado) buscando **actividad sospechosa que podría indicar información privilegiada (insider trading)**.

### ¿Qué hace exactamente?

1. **Escanea** todos los trades de Polymarket cada 60 segundos (configurable)
2. **Analiza** cada trade usando hasta 18 señales diferentes de detección
3. **Asigna un score** a cada trade según cuántas señales activa
4. **Envía alertas a Telegram** cuando un trade supera el umbral de score configurado
5. **Trackea resultados** para medir si las alertas fueron correctas (hit rate)
6. **Detecta arbitraje crypto** comparando precios de Binance con odds de Polymarket (módulo opcional)

### ¿Qué NO hace?

- **No ejecuta trades automáticamente** — solo alerta
- **No garantiza ganancias** — las alertas son anomalías estadísticas, no certezas
- **No accede a wallets ni fondos** — es un observador pasivo de datos públicos

---

## 2. Cómo Funciona el Bot

### Flujo de Procesamiento de un Trade

```
Polymarket Data API
        │
        ▼
  Obtener 500 trades recientes
        │
        ▼
  ¿Trade ya procesado? ──Sí──► Ignorar (deduplicación)
        │ No
        ▼
  Registrar trade en DB + actualizar stats de wallet
        │
        ▼
  ¿Size >= MIN_SIZE_USD? ──No──► Ignorar (muy pequeño)
        │ Sí
        ▼
  Obtener contexto:
  • Stats de la wallet
  • Baseline del mercado
  • Cluster de wallets similares
  • Precio actual del mercado
  • Info de acumulación
  • Smart wallets en el mismo lado
        │
        ▼
  ANALIZAR: Evaluar 18 señales → Score total
        │
        ▼
  ¿Score >= ALERT_THRESHOLD? ──No──► Ignorar
        │ Sí
        ▼
  ¿Categoría excluida? ──Sí──► Ignorar
        │ No
        ▼
  ¿Cooldown activo? ──Sí──► Ignorar (ya alertamos esta wallet+mercado)
        │ No
        ▼
  📱 ENVIAR ALERTA A TELEGRAM
  📊 Registrar en DB para tracking
```

### Ciclo de Polling

El bot ejecuta tareas periódicas según un ciclo base (por defecto 60 segundos):

| Frecuencia | Tarea | Descripción |
|------------|-------|-------------|
| Cada ciclo (~1 min) | **Poll trades** | Obtiene y analiza trades nuevos |
| Cada 3 ciclos (~3 min) | **Crypto signals** | Procesa señales del módulo Crypto Arb |
| Cada 5 ciclos (~5 min) | **Price impact** | Verifica movimiento de precio post-alerta |
| Cada 10 ciclos (~10 min) | **Baselines** | Actualiza estadísticas base de mercados |
| Cada 10 ciclos (~10 min) | **Smart money scores** | Recalcula scores de wallets inteligentes |
| Cada 10 ciclos (~10 min) | **Resolver crypto** | Verifica resultados de señales crypto |
| Cada 15 ciclos (~15 min) | **On-chain check** | Consulta Polygonscan para datos de wallets |
| Cada 30 ciclos (~30 min) | **Resoluciones** | Verifica si mercados se resolvieron |
| Cada 30 ciclos (~30 min) | **Watchlist** | Actualiza lista de smart wallets |
| Cada 60 ciclos (~1 hora) | **Health check** | Envía resumen de salud a Telegram |
| Cada 60 ciclos (~1 hora) | **Coordinación** | Detecta wallets coordinadas |

---

## 3. Dashboard Web

El bot incluye un dashboard web accesible desde el navegador donde se puede:

### Pestañas Disponibles

| Pestaña | Función |
|---------|---------|
| **Alertas** | Lista de todas las alertas enviadas con su score, señales, wallet, y precio |
| **Leaderboard** | Ranking de wallets más activas por volumen y trades |
| **Wallets** | Información detallada de wallets individuales |
| **Mercados** | Mercados siendo monitoreados activamente |
| **Coordinación** | Pares de wallets que actúan de forma coordinada |
| **Feed** | Feed en tiempo real de trades grandes |
| **Crypto Arb** | Panel del módulo de arbitraje crypto (precios, señales, backtest) |

### Estadísticas del Dashboard (Header)

| Métrica | Descripción |
|---------|-------------|
| **Alertas Totales** | Total de alertas enviadas desde que se creó la DB |
| **Alertas 24h** | Alertas de las últimas 24 horas |
| **Hit Rate** | Porcentaje de alertas que resultaron correctas (mercado resuelto a favor) |
| **Score Promedio** | Score medio de las alertas enviadas |
| **Trades Procesados** | Total de trades analizados (histórico) |
| **Wallets Flaggeadas** | Cantidad de wallets únicas que han disparado alertas |
| **Correctas** | Número de alertas verificadas como correctas |
| **Watchlist** | Wallets en la lista de smart money |
| **Sesión** | Trades procesados desde el último reinicio |

### Botón Config

Desde el dashboard se puede abrir el panel de configuración para ajustar TODOS los parámetros del bot en tiempo real, sin necesidad de reiniciar. Los cambios se guardan en la base de datos y persisten entre reinicios.

---

## 4. Sistema de Scoring — Las 18 Señales

Cada trade se evalúa contra hasta 18 señales. Cada señal que se activa suma puntos al score del trade. Si el score total alcanza o supera el **Alert Threshold**, se envía una alerta.

### Señales Básicas (1-7) — Siempre activas

#### Señal 1: Wallet Nueva (Fresh Wallet)
- **Emoji:** 🆕
- **Puntos por defecto:** 2
- **Parámetro:** `fresh_wallet_points`
- **Condición:** La wallet tiene ≤5 trades totales O fue vista por primera vez hace ≤30 días
- **Por qué importa:** Las wallets nuevas que aparecen con trades grandes pueden ser wallets creadas específicamente para operar con información privilegiada

#### Señal 2: Trade Grande (Large Size)
- **Emoji:** 💰
- **Puntos por defecto:** 4
- **Parámetro:** `large_size_points`
- **Condición:** El trade es ≥ `large_size_usd` (por defecto $8,000)
- **Por qué importa:** Trades de gran tamaño en mercados de predicción suelen indicar alta convicción, posiblemente basada en información privada
- **IMPORTANTE:** Si los puntos de esta señal son iguales o mayores al Alert Threshold, cualquier trade grande alertará automáticamente sin necesidad de otra señal

#### Señal 3: Anomalía de Mercado (Market Anomaly)
- **Emoji:** 📊
- **Puntos por defecto:** 2
- **Parámetro:** `market_anomaly_points`
- **Condición:** El trade supera el percentil 95 (configurable) del tamaño típico de trades en ese mercado específico
- **Por qué importa:** Un trade que es anormalmente grande comparado con la actividad habitual del mercado es estadísticamente sospechoso

#### Señal 4: Cambio de Comportamiento (Wallet Shift)
- **Emoji:** 🔄
- **Puntos por defecto:** 3
- **Parámetro:** `wallet_shift_points`
- **Condición:** El trade es ≥5x el promedio histórico de esa wallet
- **Por qué importa:** Cuando una wallet que normalmente opera con $200 de repente pone $5,000, algo cambió. Puede indicar que obtuvo información nueva

#### Señal 5: Alta Concentración (Concentration)
- **Emoji:** 🎯
- **Puntos por defecto:** 3
- **Parámetro:** `concentration_points`
- **Condición:** El trade representa ≥10% del volumen total del mercado
- **Por qué importa:** Un solo trade que mueve un porcentaje significativo del mercado sugiere alta convicción

#### Señal 6: Proximidad al Cierre (Time Proximity)
- **Emoji:** ⏰
- **Puntos por defecto:** 3
- **Parámetro:** `time_proximity_points`
- **Condición:** El mercado cierra en ≤7 días
- **Por qué importa:** Apuestas grandes cerca del cierre del mercado son más significativas porque hay menos tiempo para que el precio se mueva por fundamentales; la persona probablemente ya sabe el resultado

#### Señal 7: Cluster de Wallets
- **Emoji:** 👥
- **Puntos por defecto:** 3
- **Parámetro:** `cluster_points`
- **Condición:** 3 o más wallets diferentes apostaron en el mismo lado del mismo mercado en los últimos 30 minutos, con trades ≥$1,000
- **Por qué importa:** Múltiples wallets apostando igual al mismo tiempo sugiere acción coordinada o información compartida

### Señales Avanzadas (8-14)

#### Señal 8: Hit Rate Alto
- **Emoji:** 🏆
- **Puntos por defecto:** 2
- **Parámetros:** `hit_rate_points`, `hit_rate_min_resolved` (mín. 3), `hit_rate_min_pct` (mín. 70%)
- **Condición:** La wallet tiene ≥3 mercados resueltos y un win rate ≥70%
- **Por qué importa:** Una wallet con historial ganador tiene más probabilidad de tener información acertada

#### Señal 9: Contrarian
- **Emoji:** 🔥
- **Puntos por defecto:** 3
- **Parámetro:** `contrarian_points`
- **Condición:** La wallet apuesta CONTRA el consenso del mercado (ejemplo: compra "Sí" cuando el mercado dice 80%+ que "No")
- **Por qué importa:** Ir contra la mayoría con dinero grande requiere mucha confianza — posiblemente información que el mercado no tiene

#### Señal 10: Acumulación
- **Emoji:** 📈
- **Puntos por defecto:** 2
- **Parámetro:** `accumulation_points`
- **Condición:** La wallet ha comprado el mismo outcome en el mismo mercado 2 o más veces
- **Por qué importa:** Compras repetidas en el mismo mercado sugieren convicción creciente o building de posición

#### Señal 11: Ganador Probado (Proven Winner)
- **Emoji:** ✅
- **Puntos por defecto:** 3
- **Parámetros:** `proven_winner_points`, `proven_winner_min_resolved` (mín. 5), `proven_winner_min_pct` (mín. 65%)
- **Condición:** La wallet tiene ≥5 mercados resueltos y win rate ≥65%
- **Por qué importa:** Versión más estricta del Hit Rate — esta wallet ha demostrado consistencia ganadora

#### Señal 12: Confirmación Multi-Smart
- **Emoji:** 🧠
- **Puntos por defecto:** 3
- **Parámetro:** `multi_smart_points`
- **Condición:** 2 o más wallets "inteligentes" (con buen historial) están apostando en el mismo lado
- **Por qué importa:** Cuando múltiples traders exitosos coinciden, la señal se refuerza

#### Señal 13: Late Insider
- **Emoji:** 🕵️
- **Puntos por defecto:** 2
- **Parámetro:** `late_insider_points`
- **Condición:** Trade grande + wallet nueva + mercado cierra en ≤2 días
- **Por qué importa:** Combinación de banderas rojas: wallet nueva aparece justo antes del cierre con un trade grande. Patrón clásico de insider trading

#### Señal 14: Exit Alert (Smart Money Vendiendo)
- **Emoji:** 🚪
- **Puntos por defecto:** 2
- **Parámetros:** `exit_alert_points`, `exit_alert_min_resolved` (mín. 3), `exit_alert_min_pct` (mín. 60%)
- **Condición:** Una wallet con buen historial está VENDIENDO (saliendo de posición)
- **Por qué importa:** Si un trader exitoso cierra su posición, puede ser señal de que algo cambió. Útil como señal de salida si tú ya estás en ese mercado

### Señales de Módulos Opcionales (15-18)

Estas señales solo se activan si el módulo correspondiente está habilitado en la sección **Módulos Activos** del dashboard.

#### Señal 15: Orderbook Depth (Módulo: Orderbook Depth)
- **Emoji:** 📕
- **Puntos por defecto:** 2
- **Parámetros:** `orderbook_depth_points`, `ob_min_depth_pct` (mín. 2%)
- **Condición:** El trade consume ≥2% de la profundidad del libro de órdenes del mercado
- **Por qué importa:** Un trade que consume un porcentaje significativo del orderbook causa slippage y mueve el precio, indicando alta agresividad

#### Señal 16: Mercado Nicho (Módulo: Market Classification)
- **Emoji:** 🔬
- **Puntos por defecto:** 2
- **Parámetros:** `niche_market_points`, `niche_max_liquidity` (máx. $50,000), `niche_score_multiplier` (1.5x)
- **Condición:** El mercado tiene liquidez < $50,000 Y no es de categoría mainstream (política, deportes, crypto-prices)
- **Por qué importa:** En mercados pequeños y especializados, es más probable que un trade grande venga de alguien con información real, porque hay menos participantes y menos liquidez

#### Señal 17: Wallet Basket Shift (Módulo: Wallet Baskets)
- **Emoji:** 🔀
- **Puntos por defecto:** 3 (+2 bonus)
- **Parámetros:** `basket_points`, `basket_min_trades` (mín. 5), `basket_shift_threshold` (15%), `basket_cross_min` (2), `cross_basket_extra_points` (2)
- **Condición:** La wallet normalmente opera en una categoría (ej: política) pero ahora aparece en otra categoría diferente donde tiene <15% de su actividad
- **Bonus:** Si 2+ wallets "fuera de categoría" operan en el mismo mercado, se suman puntos extra
- **Por qué importa:** Cuando un trader que siempre apuesta en política de repente aparece en un mercado de tecnología, sugiere que obtuvo información específica sobre ese tema

#### Señal 18: Sniper DBSCAN (Módulo: Sniper DBSCAN)
- **Emoji:** 🔫
- **Puntos por defecto:** 4
- **Parámetros:** `sniper_points`, `sniper_time_window` (120s), `sniper_min_cluster` (3), `sniper_min_size` ($500)
- **Condición:** 3+ wallets diferentes ejecutaron trades ≥$500 en el mismo mercado y mismo lado, todos dentro de una ventana de 120 segundos
- **Por qué importa:** Un cluster temporal muy estrecho de múltiples wallets sugiere una acción coordinada tipo "bot sniper" — posiblemente un grupo que recibió la misma información al mismo tiempo

---

## 5. Configuración General

Todos estos parámetros se pueden ajustar desde el panel **Config** del dashboard.

### Detección General

| Parámetro | Dashboard | Default | Descripción |
|-----------|-----------|---------|-------------|
| `min_size_usd` | Min Trade Size (USD) | $2,000 | **Tamaño mínimo de trade para analizar.** Trades menores a este valor se ignoran completamente (excepto wallets en watchlist). Bajar este valor = más trades analizados pero más ruido. Subir = menos alertas pero más relevantes |
| `large_size_usd` | Large Trade Size (USD) | $8,000 | **Umbral para activar la señal "Trade Grande" (#2).** Un trade de este tamaño o mayor activa la señal y suma los puntos correspondientes |
| `alert_threshold` | Alert Threshold (Score min) | 4 | **Score mínimo para enviar alerta.** Solo los trades que acumulan este score o más generan una alerta a Telegram. Bajar = más alertas (más ruido). Subir = menos alertas (más selectivo) |
| `poll_interval` | Intervalo Polling (seg) | 60 | **Segundos entre cada ciclo de polling.** Cada ciclo consulta 500 trades nuevos. Bajar = monitoreo más frecuente pero más carga |
| `max_markets` | Max Mercados/Ciclo | 100 | **Máximo de mercados a cachear por ciclo.** Se usan para enriquecer trades con datos del mercado (categoría, fecha de cierre, etc.) |
| `cooldown_hours` | Cooldown Hours | 6 | **Horas de cooldown entre alertas de la misma wallet+mercado.** Evita spam. Si una wallet ya generó alerta en un mercado, no se alerta de nuevo hasta que pasen X horas |

### Puntos por Señal (1-7)

| Parámetro | Dashboard | Default | Señal |
|-----------|-----------|---------|-------|
| `fresh_wallet_points` | Fresh Wallet | 2 | #1 — Wallet nueva |
| `large_size_points` | Large Size | 4 | #2 — Trade grande |
| `market_anomaly_points` | Mkt Anomaly | 2 | #3 — Anomalía mercado |
| `wallet_shift_points` | Wallet Shift | 3 | #4 — Cambio comportamiento |
| `concentration_points` | Concentr. | 3 | #5 — Alta concentración |
| `time_proximity_points` | Proximidad | 3 | #6 — Cerca del cierre |
| `cluster_points` | Cluster | 3 | #7 — Cluster wallets |

### Señales Avanzadas (8-14)

| Parámetro | Dashboard | Default | Señal |
|-----------|-----------|---------|-------|
| `hit_rate_points` | Hit Rate pts | 2 | #8 — Hit rate alto |
| `hit_rate_min_resolved` | Min WR% (#8) | 3 | Mín. mercados resueltos para evaluar hit rate |
| `hit_rate_min_pct` | (implícito) | 70 | Mín. % de win rate para activar señal |
| `contrarian_points` | Contrarian pts | 3 | #9 — Apuesta contra consenso |
| `accumulation_points` | Acumulación pts | 2 | #10 — Compras repetidas |
| `proven_winner_points` | Proven Winner pts | 3 | #11 — Ganador verificado |
| `proven_winner_min_resolved` | Min WR% (#11) | 5 | Mín. mercados resueltos para proven winner |
| `proven_winner_min_pct` | (implícito) | 65 | Mín. % de win rate |
| `multi_smart_points` | Multi-Smart pts | 3 | #12 — Múltiples smart wallets |
| `late_insider_points` | Late Insider pts | 2 | #13 — Insider tardío |
| `exit_alert_points` | Exit Alert pts | 2 | #14 — Smart money sale |
| `exit_alert_min_resolved` | (implícito) | 3 | Mín. mercados resueltos para exit alert |
| `exit_alert_min_pct` | (implícito) | 60 | Mín. % win rate para exit alert |
| `cross_basket_extra_points` | Cross-Basket pts | 2 | Bonus por múltiples wallets cross-category |

### Smart Money

| Parámetro | Dashboard | Default | Descripción |
|-----------|-----------|---------|-------------|
| `smart_wallet_min_winrate` | Smart WR min | 0.55 | Win rate mínimo (0-1) para que una wallet sea considerada "smart money" y entre en la watchlist |

---

## 6. Módulos Opcionales

Los módulos se activan/desactivan desde la sección **Módulos Activos** del dashboard. Cada módulo agrega señales adicionales al sistema de scoring.

### Orderbook Depth

**Qué hace:** Consulta el libro de órdenes de cada mercado en Polymarket y calcula qué porcentaje de la profundidad consume el trade.

| Parámetro | Dashboard | Default | Descripción |
|-----------|-----------|---------|-------------|
| `ob_min_depth_pct` | Min Depth (%) | 2.0 | Mín. % del book que debe consumir el trade para activar la señal |
| `orderbook_depth_points` | Puntos Orderbook | 2 | Puntos sumados al score |

### Market Classification (Nicho vs Mainstream)

**Qué hace:** Clasifica mercados como "nicho" (poca liquidez, categoría especializada) o "mainstream" (política, deportes, crypto con mucha liquidez). Los trades en mercados nicho son más significativos.

| Parámetro | Dashboard | Default | Descripción |
|-----------|-----------|---------|-------------|
| `niche_max_liquidity` | Nicho Max Liquidez ($) | 50,000 | Liquidez máxima para considerar un mercado como "nicho" |
| `niche_score_multiplier` | Nicho Score Mult. | 1.5 | Multiplicador de puntos para mercados nicho |
| `niche_market_points` | Puntos Nicho Mkt | 2 | Puntos sumados al score |

### Wallet Baskets

**Qué hace:** Construye un "perfil de categoría" para cada wallet según su historial (ej: 60% política, 30% tecnología, 10% entretenimiento). Cuando una wallet opera fuera de su categoría habitual, es más sospechoso.

| Parámetro | Dashboard | Default | Descripción |
|-----------|-----------|---------|-------------|
| `basket_min_trades` | Basket Min Trades | 5 | Mín. trades para que una wallet tenga perfil de categoría |
| `basket_shift_threshold` | Basket Shift Threshold | 0.15 | Si la wallet tiene <15% de actividad en esta categoría, se considera "fuera de categoría" |
| `basket_points` | Basket Points | 3 | Puntos por operar fuera de categoría |
| `basket_cross_min` | Basket Cross Min | 2 | Mín. wallets fuera de categoría en el mismo mercado para bonus |
| `cross_basket_extra_points` | Cross-Basket pts | 2 | Puntos extra si múltiples wallets cross-category coinciden |

### Sniper DBSCAN

**Qué hace:** Usa un algoritmo de clustering temporal (DBSCAN simplificado) para detectar grupos de wallets que ejecutan trades casi simultáneamente en el mismo mercado.

| Parámetro | Dashboard | Default | Descripción |
|-----------|-----------|---------|-------------|
| `sniper_time_window` | Sniper Time Window (seg) | 120 | Ventana temporal en segundos: trades separados por menos de esto se agrupan |
| `sniper_min_cluster` | Sniper Min Cluster | 3 | Mín. wallets únicas en el cluster para activar señal |
| `sniper_min_size` | Sniper Min Size ($) | 500 | Mín. tamaño de trade para incluir en el escaneo sniper |
| `sniper_points` | Sniper Points | 4 | Puntos sumados (alto porque es señal fuerte) |

---

## 7. Crypto Arb — Bot de Arbitraje Crypto

### ¿Qué es?

El módulo **Crypto Arb** es un sistema separado que detecta oportunidades de arbitraje entre **precios spot de Binance** (BTC, ETH, SOL) y **odds de Polymarket** en mercados de tipo "crypto up/down 15-min".

### ¿Cómo funciona?

1. **Binance Feed:** Se conecta via WebSocket a Binance para recibir precios en tiempo real de BTC/USDT, ETH/USDT y SOL/USDT
2. **Escaneo de mercados:** Busca en Polymarket mercados activos tipo "Will Bitcoin go up or down in 15 min?"
3. **Detección de divergencia:** Compara el movimiento spot real con las odds de Polymarket. Si Bitcoin subió 0.3% pero Polymarket dice 50/50, hay una divergencia
4. **Señal:** Genera señal con dirección (UP/DOWN), confianza, edge estimado y profit esperado
5. **Paper trading:** Registra cada señal con una apuesta simulada para trackear performance

### Configuración Crypto Arb

Estos parámetros se configuran desde la pestaña **Crypto Arb → Config** del dashboard:

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| `crypto_min_move_pct` | 0.15% | Movimiento mínimo en el precio spot para considerar una divergencia |
| `crypto_max_poly_odds` | 0.65 | Odds máximas de Polymarket — si ya están por encima de esto, no hay edge suficiente |
| `crypto_min_confidence` | 70% | Confianza mínima para generar señal |
| `crypto_min_time_sec` | 120s | Tiempo mínimo restante del mercado (si cierra en <2min, muy arriesgado) |
| `crypto_max_time_sec` | 720s | Tiempo máximo restante (si faltan >12min, el precio puede revertir) |
| `crypto_paper_bet` | $100 | Tamaño de apuesta simulada para tracking de PnL |
| `crypto_max_daily` | 50 | Máximo de señales por día para evitar over-trading |
| `crypto_telegram` | true | Enviar señales crypto a Telegram |

### Dashboard Crypto Arb

La pestaña **Crypto Arb** del dashboard tiene sub-secciones:

- **Stats:** Señales hoy, win rate, PnL acumulado, drawdown
- **Precios:** Precios en tiempo real de BTC, ETH, SOL con momentum
- **Live:** Señales activas en este momento
- **Historial:** Todas las señales pasadas con su resultado
- **Backtest:** Simulador para probar la estrategia con datos históricos reales
- **Config:** Ajustar parámetros del módulo

### Backtester

El backtester permite simular la estrategia crypto arb con datos históricos reales:

1. **Seleccionar periodo:** 7 días, 30 días, etc.
2. **Configurar:** Tamaño de apuesta y max odds
3. **Ejecutar:** El bot obtiene klines reales de Binance y resoluciones reales de mercados de Polymarket
4. **Resultados:** Win rate, PnL, trades por día, drawdown máximo

---

## 8. Alertas de Telegram

### Tipos de Alerta

#### 1. Alerta de Actividad Sospechosa (principal)
```
🚨 Actividad Sospechosa Detectada

Mercado: Will X happen by date?
Apuesta: Yes BUY | Size: $5,000 | Precio: 0.35

Wallet: 0x7b19...98f5 (3 trades)
Score: [████████░░░░] 9/38

Señales:
  • 🆕 Wallet nueva (3tx)
  • 💰 Grande $5,000
  • 📊 Anomalía mercado
  • 🎯 Alta concentración

📅 Cierra en 3.5 días
📈 Hit rate wallet: 75%

🔗 Ver en Polymarket
⚠️ Alerta de anomalía. DYOR.
```

#### 2. Smart Money Alert (Copy Trade)
Se envía cuando una wallet de la watchlist (smart money verificado) realiza un trade:
```
⭐ Smart Money Alert — Copy Trade

Mercado: ...
Wallet: 0xabc1...def4
Score: [██████░░░░░░] 6/38

💡 Wallet en watchlist por rendimiento histórico.
```

#### 3. Señal Crypto Arb
```
🟢 Crypto Arb Signal — BTC UP

Spot: $97,500.00 (+0.250%)
Polymarket: 0.48 → Fair: 0.72
Edge: 24.0% | Profit est: 108%

Confianza: [███████░░░] 75%
Cierra en: 8m 30s

💡 Señal automática. Paper trading.
```

#### 4. Health Check (cada ~1 hora)
```
📊 Health Check

• Trades procesados: 15,230
• Alertas totales: 45
• Alertas 24h: 12
• Hit rate: 62.5%
• Wallets flaggeadas: 38
• Score promedio: 7.2
• Uptime: 5h 30m
```

#### 5. Resolución de Mercado
```
✅ Mercado Resuelto

Market: Will X happen?
Resolución: Yes
Alertas correctas: 3/4
```

### Barra de Score

La barra visual `[████████░░░░] 9/38` muestra:
- **9** = score del trade
- **38** = máximo score teórico posible (suma de todos los puntos de todas las señales activas)
- Los bloques llenos (█) representan el porcentaje del score máximo alcanzado

---

## 9. Smart Money y Watchlist

### ¿Qué es la Watchlist?

La watchlist es una lista automática de wallets consideradas "smart money" — traders con historial verificado de aciertos. Se actualiza cada 30 minutos.

### Criterios para entrar a la Watchlist

Una wallet entra a la watchlist cuando:
1. Su **smart money score** supera el umbral (default: 50)
2. Tiene al menos **3 mercados resueltos** (configurable via `COPY_TRADE_MIN_RESOLVED`)
3. Su **win rate** es ≥ 55% (configurable via `smart_wallet_min_winrate`)

### ¿Qué pasa con wallets en Watchlist?

- Sus trades se procesan **incluso si son menores a MIN_SIZE_USD**
- Si su score no alcanza el threshold, se envía igualmente como **Copy Trade Alert**
- Se marcan con ⭐ en la alerta de Telegram

---

## 10. Categorías Excluidas

Desde el dashboard se pueden excluir categorías completas de mercados. Los trades en estas categorías **no generarán alertas** incluso si su score supera el threshold.

### Categorías Disponibles

| Categoría | Descripción |
|-----------|-------------|
| `politics` | Mercados de política (elecciones, legislación) |
| `sports` | Deportes en general |
| `nba` | NBA Basketball |
| `nfl` | NFL Football |
| `nhl` | NHL Hockey |
| `mlb` | MLB Baseball |
| `mls` | MLS Soccer |
| `soccer` | Fútbol internacional |
| `esports` | Esports/Gaming |
| `crypto-prices` | Precios de crypto (mercados de precio tipo "Bitcoin above $X") |
| `crypto-price` | Variante del anterior |
| `updown` | Mercados up/down de corto plazo |
| `entertainment` | Entretenimiento |
| `science` | Ciencia |
| `technology` | Tecnología |
| `business` | Negocios |
| `culture` | Cultura |
| `pop-culture` | Cultura pop |

### Cómo funciona el filtrado

El bot filtra por categoría de 3 formas:
1. **Categoría directa:** Si el mercado tiene categoría asignada que está en excluidas
2. **Slug del mercado:** Si el slug (URL) contiene alguna categoría excluida como substring
3. **Keywords en pregunta:** Si la pregunta del mercado contiene keywords de deportes excluidos

### Recomendación

Se recomienda excluir al menos `sports, nba, nfl, nhl, mlb, mls, soccer, esports` porque:
- Los resultados deportivos son públicos y no representan insider trading
- Generan mucho volumen de trades (30-50% del total) que diluyen las señales reales

---

## 11. Guía de Configuración Recomendada

### Configuración Conservadora (pocas alertas, alta calidad)

```
Min Trade Size:     $5,000
Large Trade Size:   $15,000
Alert Threshold:    6
Large Size Points:  2
Cooldown:           8 horas
```
**Resultado esperado:** 2-5 alertas/día en horario activo

### Configuración Moderada (balance)

```
Min Trade Size:     $2,000
Large Trade Size:   $8,000
Alert Threshold:    4
Large Size Points:  3
Cooldown:           6 horas
```
**Resultado esperado:** 5-15 alertas/día en horario activo

### Configuración Agresiva (muchas alertas)

```
Min Trade Size:     $1,000
Large Trade Size:   $5,000
Alert Threshold:    3
Large Size Points:  3
Cooldown:           3 horas
```
**Resultado esperado:** 15-40 alertas/día. Más ruido pero menos probabilidad de perder señales reales

### Relación entre Large Size Points y Alert Threshold

**REGLA CLAVE:** Si `large_size_points >= alert_threshold`, CUALQUIER trade ≥ `large_size_usd` generará una alerta automáticamente sin necesidad de ninguna otra señal. Esto puede ser deseable (capturar todos los trades grandes) o no (demasiado ruido).

### Horarios de Mayor Actividad

Los mercados de Polymarket son más activos durante:
- **10:00 AM - 8:00 PM EST** (horario de mercados US)
- **Días laborales** (lunes a viernes)
- **Eventos importantes** (elecciones, decisiones de la Fed, etc.)

En horarios fuera de pico (noche, fines de semana), el volumen de trades puede bajar 70-90%, resultando en muchas menos alertas.

---

## 12. Preguntas Frecuentes

### ¿Por qué no recibo alertas?

**Causas comunes:**
1. **Horario de bajo volumen:** Noches/fines de semana tienen muy poco volumen
2. **MIN_SIZE muy alto:** Si está en $5,000+, muy pocos trades pasan el filtro
3. **ALERT_THRESHOLD muy alto:** Con threshold 7+, se necesitan muchas señales simultáneas
4. **Cooldown activo:** Si las mismas wallets/mercados ya alertaron en las últimas X horas
5. **Categorías excluidas:** Si excluiste muchas categorías, hay menos mercados disponibles

**Diagnóstico rápido:** Mira el contador "SESIÓN" en el dashboard. Si sube, el bot está procesando trades. Si no genera alertas, ajusta los umbrales.

### ¿Qué significa el Hit Rate?

El hit rate mide qué porcentaje de tus alertas resultaron ser correctas. Se calcula automáticamente cuando los mercados se resuelven:
- **≥60%:** Excelente — las señales son precisas
- **50-60%:** Bueno — mejor que azar
- **<50%:** Los parámetros necesitan ajuste

### ¿El bot puede equivocarse?

Sí. Las alertas son anomalías estadísticas, no certezas. Un trade puede activar muchas señales y aún así estar equivocado. El bot es una herramienta de filtrado, no un oráculo. **Siempre haz tu propia investigación (DYOR).**

### ¿Qué pasa cuando el bot se reinicia?

Toda la configuración se guarda en PostgreSQL, así que los cambios hechos desde el dashboard persisten entre reinicios. El bot restaura automáticamente toda la config al arrancar. Lo único que se pierde es:
- El contador de "SESIÓN" (se reinicia)
- Las conexiones WebSocket (se reconectan automáticamente)

### ¿Puedo poner todos los puntos en 0?

Sí. Si pones los puntos de una señal en 0, esa señal sigue evaluándose pero no suma al score. Efectivamente la desactivas sin perder la info en los triggers.

### ¿Cómo sé si el bot está funcionando?

1. **Dashboard:** El contador "SESIÓN" debe subir cada minuto
2. **Telegram:** Recibes un health check cada ~1 hora
3. **Endpoint /api/health:** Devuelve el estado del bot

---

*Este manual cubre la versión 6.0 del Polymarket Insider Alert Bot. Para soporte o preguntas, consulta el código fuente o el dashboard web.*
