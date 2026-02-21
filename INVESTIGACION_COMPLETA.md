# 🔬 INVESTIGACIÓN COMPLETA — Cómo un Bot Puede Ser Rentable en Polymarket

> **Fecha**: 21 Feb 2026
> **Objetivo**: Entender por qué perdemos, qué hacen los bots exitosos, y qué estrategia seguir.

---

## PARTE 1: TODO LO QUE HEMOS CONSTRUIDO

### Inventario completo del bot (48 archivos Python):

| Módulo | Archivo | Estado | Funciona? |
|--------|---------|--------|-----------|
| **Sniper (principal)** | `crypto_arb/autotrader.py` (1662 líneas) | Activo, modo real | ❌ Pierde dinero |
| **Detector** | `crypto_arb/detector.py` (1154 líneas) | Activo | ⚠️ fair_odds inventados |
| **Market Maker bilateral** | `strategies/market_maker.py` (970 líneas) | Implementado completo | ❌ Órdenes no se llenan |
| **Paper Trader maker** | `crypto_arb/paper_trader.py` (431 líneas) | Activo (paper) | ✅ Solo simula |
| **Early Entry** | `crypto_arb/early_detector.py` (586 líneas) | Implementado | ⚠️ No probado en real |
| **Complement Arb (YES+NO<$1)** | `strategies/complement_arb.py` (187 líneas) | Implementado | ⚠️ Scanner, no ejecuta |
| **Spike Detector** | `strategies/spike_detector.py` (102 líneas) | Esqueleto | ❌ Básico |
| **Cross-Platform Arb** | `strategies/cross_platform.py` (128 líneas) | Esqueleto | ❌ No conectado |
| **Event-Driven** | `strategies/event_driven.py` (132 líneas) | Esqueleto | ❌ Básico |
| **Copy Trading** | `trading/copy_engine.py` (106 líneas) | Implementado | ⚠️ Simulado |
| **Binance Feed** | `crypto_arb/binance_feed.py` (489 líneas) | Activo | ✅ Funciona bien |
| **Backtester** | `crypto_arb/backtester.py` | Existe | ⚠️ No verificado |
| **Config** | `config.py` (600 líneas) | Activo | ✅ Masivo |
| **Dashboard** | `dashboard/index.html` | Activo | ✅ Funciona |

### ¿Por qué el Market Maker NO funcionó?

Leí las 970 líneas de `market_maker.py`. El código está bien implementado pero tiene 
**3 problemas estructurales** que impiden que funcione:

**1. Las órdenes GTC se posteaban demasiado lejos del ask:**
```
spread_target = 0.04  (4 centavos por debajo)
bid_up = up_price - spread/2 + bias  → 2 centavos abajo del ask
```
En mercados de 5-15 minutos, 2-4 centavos debajo del ask es DEMASIADO.
Los market makers profesionales postean a 0.5-1 centavo del ask.
A 4 centavos, nadie vende contra tu orden porque hay mejores precios arriba.

**2. El py-clob-client NO soporta verdadero post-only:**
El código usa `OrderType.GTC`, pero GTC en Polymarket puede matchear 
inmediatamente si cruzas el spread (se convierte en taker).
No existe un flag "post-only" en la API Python. Para garantizar maker,
tu precio debe estar ESTRICTAMENTE por debajo del best ask.

**3. Timing incorrecto para bilateral:**
El bot intenta llenar AMBOS lados (Up + Down) del mismo mercado.
Pero en mercados de 5-15 min, el precio se mueve rápido:
- Posteamos Up@0.48 y Down@0.48
- Si BTC sube, Up se mueve a 0.60 y Down a 0.40
- Nuestro bid de Up@0.48 NUNCA se llena (mercado está 12 centavos arriba)
- Nuestro bid de Down@0.48 podría llenarse, pero ahora Down vale 0.40
  → estamos comprando a 0.48 algo que vale 0.40 → PÉRDIDA

Para que bilateral funcione, necesitas mercados estables (prices ~0.50)
y re-quoting CADA SEGUNDO, no cada 10 segundos.

---

## PARTE 2: CÓMO LOS BOTS EXITOSOS GANAN DINERO

### Investigación de 10+ fuentes (GitHub, CoinDesk, Reddit, papers, docs oficiales):

### 🏆 Estrategia #1: Complement Arbitrage (YES + NO < $1) — LA MÁS EXITOSA

**Fuentes**: CoinDesk (hoy, 21 Feb 2026), IMDEA Networks study, Reddit r/algotrading

**Cómo funciona:**
- En mercados binarios, YES + NO deberían sumar $1.00
- Por microsegundos, la suma cae a $0.97-$0.98
- Compras AMBOS lados → garantizado $1.00 al resolver → profit $0.02-$0.03

**Resultados REALES documentados:**
- Bot de CoinDesk: **8,894 trades, ~$150,000 profit** en mercados 5min crypto
- $16.80 promedio por trade (1.5-3% de edge)
- Usa ~$1,000 por round-trip
- **98% win rate** (pierde solo si no llena ambos lados)

**Por qué funciona:**
- Es RISK-FREE si llenas ambos lados
- Gaps existen por: market makers retirando quotes en volatilidad, 
  retail comprando agresivamente un lado, order book imbalances
- Los gaps duran **milliseconds a seconds** — necesitas velocidad

**Lo que necesitas:**
- WebSocket del CLOB (no REST polling)
- Ejecución en <500ms
- VPS con baja latencia (no Railway)
- Capital: $200-$1000 circulante

**Ya lo tenemos parcialmente:** `complement_arb.py` escanea pero NO ejecuta.
Solo necesita: conexión al CLOB real + ejecución automática + WebSocket.

### 🏆 Estrategia #2: Cross-Platform Arb (Polymarket vs Kalshi)

**Fuentes**: GitHub (CarlosIbCu), Reddit r/algotrading, SSRN paper

**Cómo funciona:**
- Polymarket: BTC UP a $0.55 (implica 55% prob)
- Kalshi: BTC UP a $0.50 (implica 50% prob)
- Comprar YES en Kalshi ($0.50) + NO en Polymarket ($0.45) = $0.95
- Resultado garantizado: $1.00 → profit $0.05 (5.26%)

**Resultados REALES documentados:**
- 0xalberto (GitHub): $764 profit en 1 día con $200 deposit (solo BTC)
- Reddit user: spreads de 2-5% consistentes entre plataformas
- Funciona mejor en mercados 1-hour (más tiempo para ejecutar)

**Problema:**
- Necesitas cuenta en Kalshi (solo USA, regulada)
- Tiempos de settlement diferentes
- Capital partida entre 2 plataformas

### 🏆 Estrategia #3: Flash Crash / Spike Detection

**Fuentes**: discountry/polymarket-trading-bot (GitHub)

**Cómo funciona:**
- Monitorea orderbook via WebSocket en tiempo real
- Cuando probabilidad CAE 0.30+ en 10 segundos → COMPRAR (reversal)
- Take profit: +$0.10 por share
- Stop loss: -$0.05 por share

**Por qué funciona:**
- Flash crashes ocurren cuando alguien vende muchas shares de golpe
- El precio cae temporalmente por debajo del valor justo
- Se recupera en segundos → compraste barato, vendes al precio normal

**Lo que necesitas:**
- WebSocket del CLOB orderbook (crítico)
- Ejecución instantánea (FOK)
- Monitoreo 24/7

### 🏆 Estrategia #4: Momentum + Latency (lo que intenta nuestro sniper)

**Fuentes**: Reddit (fracaso documentado), IMDEA study

**ADVERTENCIA**: Esta es la estrategia que estamos usando y que PIERDE DINERO.

Un developer en Reddit documentó exactamente nuestro problema:
> "Idea was to detect momentum on Binance and front-run the Polymarket 15-min 
>  markets before they adjusted. Paper trading showed 36.7% win rate that looked 
>  promising. Result: Complete failure in live. I was using Gamma API bid prices 
>  for paper trading but CLOB API ask prices for execution. Every 'profitable' 
>  paper trade was actually a loss when accounting for the bid-ask spread."

**Por qué no funciona para nosotros:**
1. Latencia de 55-150 segundos → el mercado YA se ajustó
2. Taker fees de 1.56% comen el edge
3. Slippage del 5% destruye más edge
4. fair_odds sin base estadística real
5. **El paper trader simulaba fills que en la realidad no ocurrían**

### 🏆 Estrategia #5: Market Making puro (spread capture)

**Fuentes**: Polymarket blog (open source MM), quantjourney.substack

**Cómo funciona:**
- Postear BUY y SELL en el mismo token (o BUY Up y BUY Down)
- Capturar el spread (1-4 centavos)
- 0% fee como maker + rebates diarios

**Por qué NO funcionó para nosotros:**
- spread_target de 4 centavos = demasiado lejos del ask
- Requote cada 10 segundos = demasiado lento
- Mercados de 5-15min son demasiado volátiles para MM bilateral puro
- Sin WebSocket = no puedes reaccionar cuando el precio se mueve

**Qué necesitaría para funcionar:**
- WebSocket del CLOB orderbook (reaccionar en <1s a cambios)
- Spread de 0.5-1 centavo (no 4)
- Requote cada 1-2 segundos
- Solo mercados con >$5,000 de liquidez por lado

---

## PARTE 3: ANÁLISIS DE RENTABILIDAD — ¿QUÉ PUEDE FUNCIONAR?

### Tabla comparativa de estrategias:

| Estrategia | Edge/trade | Win rate | Freq | Capital | Infraestructura | Ya tenemos? |
|------------|-----------|----------|------|---------|-----------------|-------------|
| **Complement Arb (YES+NO<$1)** | 1.5-3% | ~98% | 20-50/día | $200-1000 | WebSocket + VPS | Scanner listo, falta ejecución |
| **Cross-Platform (Poly vs Kalshi)** | 2-5% | ~95% | 5-15/día | $500 × 2 plat | 2 APIs | Esqueleto, necesita Kalshi |
| **Flash Crash** | 5-15% | ~60% | 2-5/día | $100-500 | WebSocket orderbook | No implementado |
| **Momentum/Sniper** (actual) | -2 a 3% | ~50% | 20/día | $100+ | REST polling | ✅ Implementado, PIERDE |
| **Market Making bilateral** | 0.5-2% | ~70% | 20-50/día | $500+ | WebSocket + requote 1s | Implementado, no funciona |

### La verdad incómoda:

**Las estrategias que REALMENTE ganan dinero requieren:**
1. **WebSocket en tiempo real** (no REST polling cada 15 segundos)
2. **Ejecución en <1 segundo** (no 55 segundos de delay)
3. **VPS con baja latencia** (no Railway que agrega 100-200ms)

**Nuestro bot está fundamentalmente limitado por infraestructura:**
- Railway ejecuta en containers con latencia variable (~200ms+)
- REST polling cada 15s pierde todas las oportunidades de <15s
- Sin WebSocket del CLOB, no vemos cambios en el orderbook

---

## PARTE 4: PROPUESTA DEFINITIVA

### Opción A: Complement Arbitrage Bot (RECOMENDADA — más viable)

**Por qué esta:**
- Es la estrategia PROBADA con $150,000 en profit documentado
- Ya tenemos `complement_arb.py` con el scanner
- Ya tenemos `check_price_sum_arb()` en detector.py
- Edge es risk-free (no adivinamos dirección)
- No necesita fair_odds ni predicción
- Funciona con nuestro capital ($100-200)

**Qué falta:**
1. Conectar WebSocket del CLOB para ver orderbook en tiempo real
2. Ejecución automática (comprar ambos lados simultáneamente)
3. Optimizar para velocidad (<500ms entre detección y ejecución)
4. Migrar de Railway a un VPS barato (~$5/mes en Hetzner/Contabo)

**Riesgo principal:** Si solo llenamos 1 lado, quedamos expuestos.
Mitigación: timeout de 3s, cancelar si no llena ambos.

**Profit estimado:** $5-20/día con $200 capital = $150-600/mes

### Opción B: Flash Crash Detector (alternativa)

**Por qué:**
- Usa WebSocket para detectar caídas bruscas de precio
- Compra cuando todos venden en pánico → precio se recupera
- Edge de 5-15% por trade (menos frecuente, más profitable)
- No necesita predecir dirección, solo detectar anomalía

**Qué falta:**
1. Implementar WebSocket CLOB para monitorear orderbook
2. Algoritmo de detección de flash crash (caída >20% en <10s)
3. Ejecución FOK inmediata
4. TP/SL ajustados (+$0.10 / -$0.05)

**Profit estimado:** $10-30/día si hay 2-5 crashes

### Opción C: Mejorar Sniper actual (parche, no solución)

Lo que propuse en GUIA_FIXES_V2.md. Reduce pérdidas pero NO las elimina porque
el problema fundamental (latencia + no WebSocket) persiste.
**No recomiendo invertir más tiempo en esto** si el objetivo es ser rentable.

### Opción D: Cross-Platform Poly + Kalshi (requiere cuenta USA)

La más profitable per-trade (2-5%) pero necesitas:
- Cuenta en Kalshi (requiere residencia USA o VPN)
- Capital dividido entre 2 plataformas
- Si no tienes acceso a Kalshi → no viable

---

## PARTE 5: DECISIÓN Y SIGUIENTE PASO

### Mi recomendación: Opción A (Complement Arb) + Opción B (Flash Crash)

**Fase 1 (inmediata):** Implementar Complement Arb
- Reusar `complement_arb.py` como base
- Agregar WebSocket del CLOB (ya tenemos `infra/websocket_client.py`)
- Ejecución automática de compra bilateral
- Probar con $10-20 primero

**Fase 2 (después de validar):** Agregar Flash Crash
- Detector de caídas bruscas en el WebSocket
- Compra FOK inmediata en caídas >20%
- Complementa al arb con profit por trade más alto

**Fase 3 (opcional):** Desactivar sniper
- El sniper actual pierde dinero consistentemente
- Mantenerlo solo como PAPER para recoger datos
- Enfocarnos en las estrategias que tienen edge real

### Sobre infraestructura:
- **Railway puede servir para empezar** — el complement arb no necesita
  latencia de microsegundos, gaps de 1-3 segundos son suficientes
- Si funciona, migrar a VPS ($5/mes) para mejor latencia
- WebSocket del CLOB es el cambio más importante — ya tenemos el módulo

---

## RESUMEN EJECUTIVO

| Lo que hacemos ahora | Por qué falla |
|---------------------|--------------|
| Sniper: predecir dirección de BTC en 5-15min | No tenemos edge predictivo real |
| Taker fees en cada trade | 1.56% comen cualquier edge |
| Polling REST cada 15s | Perdemos oportunidades de <15s |
| fair_odds inventados | No reflejan realidad del mercado |
| Market maker con spread de 4¢ | Demasiado lejos, nunca se llena |

| Lo que deberíamos hacer | Por qué funciona |
|------------------------|-----------------|
| Complement Arb: comprar YES+NO cuando <$1 | Risk-free, probado, $150K profit |
| WebSocket del CLOB | Ver orderbook en tiempo real |
| Ejecución <1s | Capturar gaps antes que otros |
| Flash Crash detector | Comprar en pánico, edge 5-15% |
| 0% fee (maker orders) | Margin positivo en cada trade |

> **¿Qué opción prefieres? Cuando me digas, ajusto la guía paso a paso.**
