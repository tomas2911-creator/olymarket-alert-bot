# PLAN: Weather Trading Bot Completo (Modelo Crypto Arb)

**Fecha:** 22 de febrero 2026  
**Objetivo:** Convertir el weather arb en un bot de trading completo, al mismo nivel que el crypto arb.  
**Basado en:** Análisis de HondaCivic + arquitectura existente del crypto arb bot.

---

## 0. ESTADO ACTUAL — Qué YA existe vs Qué FALTA

### ✅ YA EXISTE (backend)
| Componente | Archivo | Estado |
|-----------|---------|--------|
| Weather Feed (Open-Meteo ensemble 31 miembros) | `weather_arb/weather_feed.py` | Funcional |
| Detector de señales (edge ensemble vs Polymarket) | `weather_arb/detector.py` | Funcional |
| AutoTrader (ejecuta trades reales via CLOB) | `weather_arb/autotrader.py` | Funcional |
| Paper Trader (tracking PnL sin dinero real) | `weather_arb/backtester.py` | Funcional |
| Config en config.py | `config.py` líneas 195-204 | Funcional |
| Main.py loops (feed + detector + signal loop) | `main.py` líneas 406-496 | Funcional |
| API Routes (stats, signals, markets, trades, paper, config, keys) | `routes.py` líneas 3799-4229 | Funcional |
| DB tabla weather_trades | `database.py` | Funcional |
| Wallet SEPARADA (prefijo "wt_") | autotrader.py | Funcional |

### ❌ FALTA (comparado con crypto arb)
| Componente | Equivalente Crypto | Prioridad |
|-----------|-------------------|-----------|
| **APIs weather adicionales** (solo tiene Open-Meteo) | Binance Feed (fuente única pero confiable) | ALTA |
| **Estrategia de eliminación** (comprar NO a 99.9¢) | No existe en crypto | ALTA |
| **Risk management loop activo** (TP/SL cada 30s) | `autotrader.check_risk_management()` | ALTA |
| **Opción usar misma wallet que crypto** | N/A (cada uno tiene su propia) | ALTA |
| **Config detector desde dashboard** | `/api/crypto-arb/config` GET/POST | MEDIA |
| **Pre-market analysis** (prepararse antes de apertura) | `early_detector.py` | ALTA |
| **Orderbook depth check** antes de tradear | `FEATURE_ORDERBOOK_CRYPTO` | MEDIA |
| **Dashboard tab Weather completo** | Tab "Crypto Arb" con subtabs | ALTA |
| **Señales en vivo con auto-refresh** | crypto live signals con polling 5s | ALTA |
| **Backtest histórico** | `crypto_arb/backtester.py` | BAJA |
| **Telegram alerts para weather** | crypto tiene Telegram alerts | MEDIA |
| **Swing trading (vender posición en profit)** | autotrader tiene sell logic | ALTA |

---

## 1. APIS WEATHER ADICIONALES — Ventaja Competitiva

### Problema actual
Solo usa **Open-Meteo Ensemble API** (31 miembros GFS). Esto limita la precisión.  
HondaCivic probablemente usa **datos en tiempo real** de estaciones meteorológicas.

### Plan: 3 fuentes adicionales (todas gratuitas)

#### 1.1 Weather.gov API (NOAA) — US cities
- **Gratis, sin API key**, datos oficiales de USA
- URL: `https://api.weather.gov/points/{lat},{lon}` → forecast URL
- Tiene **probabilidades** de temperatura por rango de horas
- Resolución horaria, pronóstico a 7 días
- **Ciudades US:** NYC, Chicago, Dallas, Atlanta, Miami, Seattle

#### 1.2 WeatherAPI.com — Global
- **Gratis hasta 1M calls/mes** (con API key gratuita)
- URL: `https://api.weatherapi.com/v1/forecast.json?key={KEY}&q={city}&days=3`
- Tiene `maxtemp_c`, `maxtemp_f` por día
- No tiene ensemble pero es alta resolución
- **Todas las ciudades** incluyendo las internacionales

#### 1.3 Visual Crossing — Global (backup)
- **Gratis hasta 1000 calls/día** (con API key gratuita)
- URL: `https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/{city}?unitGroup=us&key={KEY}`
- Tiene `tempmax` por día
- **Backup en caso de que Open-Meteo o WeatherAPI fallen**

### Implementación: `weather_arb/multi_feed.py`
```
class MultiWeatherFeed:
    """Combina múltiples fuentes de pronóstico.
    
    Para cada ciudad/fecha, obtiene pronósticos de:
    1. Open-Meteo Ensemble (31 miembros → distribución)
    2. Weather.gov (NOAA, solo US)
    3. WeatherAPI.com (global, determinístico)
    4. Visual Crossing (backup)
    
    Genera un "consensus forecast" con mayor confianza.
    """
    
    def get_consensus(city, date) -> ConsensusForecast:
        """
        - Si 3+ fuentes coinciden en ±1°: confianza ALTA (>90%)
        - Si 2 fuentes coinciden: confianza MEDIA (70-90%)
        - Si divergen: confianza BAJA (<70%), no tradear
        """
```

### ENV vars nuevas
```
WEATHERAPI_KEY=  # Gratis en weatherapi.com
VISUAL_CROSSING_KEY=  # Gratis en visualcrossing.com
# Weather.gov no necesita API key
```

---

## 2. ESTRATEGIA DE ELIMINACIÓN (Inspirada en HondaCivic)

### Concepto
HondaCivic gasta 60-70% de su volumen comprando **NO** en temperaturas imposibles a 99.9¢.  
Con $500 de capital, esto NO es viable directo (ganancia de $0.50 por $500 invertidos).

### Versión adaptada: "Smart Elimination"
En vez de eliminar a 99.9¢, buscamos rangos donde:
- **Ensemble dice 0% probabilidad** (ninguno de 31 miembros cae ahí)
- **Polymarket paga >3¢** por ese rango (YES a 3¢ = NO a 97¢)
- **Edge del NO: 97¢ → $1 = +3% profit** (mucho mejor que 0.1% de HondaCivic)

### Implementación en `detector.py`
```python
async def _check_elimination_edges(self) -> list[WeatherSignal]:
    """Buscar rangos con 0% ensemble pero >3¢ en Polymarket.
    
    Ejemplo: Buenos Aires Feb 22, ensemble dice 30-34°C.
    Si "20°C or below" tiene YES=5¢, comprar NO a 95¢ → +5% profit
    si NINGÚN miembro del ensemble está debajo de 20°C.
    """
```

### Parámetros configurables
- `elimination_enabled`: on/off
- `elimination_min_profit_pct`: 2% mínimo (default)
- `elimination_max_bet`: $50 máximo por trade de eliminación
- `elimination_require_zero_members`: true (solo si 0/31 miembros están en ese rango)

---

## 3. PRE-MARKET ANALYSIS & SPEED

### Problema
Los mercados weather de Polymarket se crean diariamente. Quien llega primero tiene mejores odds.  
HondaCivic ejecuta 11 trades/segundo = usa un bot automatizado.

### Plan: Early Weather Detector
```
class EarlyWeatherDetector:
    """Prepara análisis ANTES de que abra el mercado.
    
    1. Calcula slug esperado para mañana: 
       "highest-temperature-in-nyc-on-february-23-2026"
    2. Ejecuta forecast ensemble para mañana AHORA
    3. Pre-calcula distribución de probabilidades
    4. Cuando el mercado abre en Polymarket (scan cada 60s), 
       INMEDIATAMENTE compara forecast vs odds
    5. Si hay edge, ejecuta trade en <5 segundos
    """
```

### Flujo de velocidad
```
T-12h: Pre-calcular forecasts para mañana (todas las ciudades)
T-6h:  Refresh forecasts con datos más actuales
T-1h:  Refresh final, pre-calcular distribución por rango
T-0:   Mercado abre en Polymarket
T+5s:  Scan detecta mercado nuevo
T+10s: Comparar forecast precalculado vs odds iniciales
T+15s: Si hay edge → ejecutar trade inmediato
```

### Implementación: `weather_arb/early_weather.py`
- Hereda lógica de `crypto_arb/early_detector.py` pero adaptada
- Pre-genera slugs de mercados para los próximos 3 días
- Mantiene forecasts en memoria
- Trigger inmediato cuando scan encuentra mercado nuevo

---

## 4. RISK MANAGEMENT ACTIVO (TP/SL/Trailing)

### Problema actual
El autotrader tiene config de `stop_loss_pct` y `take_profit_pct` pero **NO HAY LOOP** que los monitoree activamente.  
En crypto, `check_risk_management()` corre cada 5 segundos.

### Implementación: `WeatherAutoTrader.check_risk_management()`
```python
async def check_risk_management(self):
    """Monitorear posiciones abiertas y ejecutar TP/SL.
    
    - Obtener precio actual de cada posición via CLOB
    - Si unrealized PnL > take_profit_pct → VENDER
    - Si unrealized PnL < -stop_loss_pct → VENDER
    - Si trailing stop activado → ajustar stop dinámicamente
    - Si tiempo en posición > max_holding_sec → VENDER
    """
```

### Venta (SELL) de posiciones
```python
async def sell_position(self, condition_id: str, reason: str):
    """Vender una posición abierta.
    
    1. Obtener token_id Yes del mercado
    2. Calcular precio de venta (bid price actual)
    3. Crear orden SELL via CLOB
    4. Registrar en DB
    """
```

### Loop en main.py
```python
# Dentro de _run_weather_signal_loop, agregar:
if self.weather_autotrader:
    await self.weather_autotrader.check_risk_management()
```

Frecuencia: cada 30 segundos (weather markets son lentos comparados con crypto 5min).

---

## 5. COMPARTIR WALLET (Crypto ↔ Weather)

### Situación actual
- Crypto usa prefijo `at_` en config DB (api_key, api_secret, private_key, passphrase, funder_address)
- Weather usa prefijo `wt_` (mismos campos)
- Son wallets independientes

### Plan: Opción "Usar misma wallet que Crypto"
```
Nuevo campo en config: wt_use_crypto_wallet = "true" | "false"

Si true:
  - Weather autotrader lee credenciales de at_api_key, at_api_secret, etc.
  - No necesita generar keys separadas
  - Comparten el mismo CLOB client
  
Si false:
  - Comportamiento actual (wallet separada con wt_*)
```

### Implementación
1. **autotrader.py**: Al inicializar, si `wt_use_crypto_wallet == "true"`, leer `at_*` keys en vez de `wt_*`
2. **routes.py**: Checkbox en config "Usar misma wallet que Crypto Arb"
3. **Dashboard**: Toggle visual en la config panel

### Beneficio
- No necesita fondear 2 wallets separadas
- Simplifica setup para el usuario

---

## 6. CONFIG DETECTOR DESDE DASHBOARD

### Crypto tiene: `/api/crypto-arb/config` (GET/POST)
Permite cambiar min_move_pct, max_poly_odds, min_confidence, etc. desde el dashboard.

### Weather necesita: `/api/weather-arb/detector-config` (GET/POST)
```python
# GET: Retorna config actual
{
    "min_edge_pct": 8.0,
    "min_confidence_pct": 50.0,
    "max_poly_odds": 0.85,
    "scan_interval_sec": 300,
    "forecast_refresh_sec": 1800,
    "paper_bet_size": 50,
    "telegram_alerts": true,
    "cities": ["nyc", "london", ...],
    "elimination_enabled": false,
    "elimination_min_profit_pct": 2.0,
    "elimination_max_bet": 50,
    "multi_source_enabled": true,
    "consensus_min_sources": 2,
    "early_detector_enabled": true,
    "pre_market_hours": 12,
}

# POST: Actualizar config
# Actualiza config.py en memoria + DB + recargar detector
```

---

## 7. DASHBOARD TAB WEATHER COMPLETO

### Estructura (copiada del crypto arb tab)

```
Tab "Weather Arb"
├── Sub-tab "Overview" (default)
│   ├── KPIs: Señales hoy, Mercados activos, Ciudades, PnL total
│   ├── Estado del feed (running, last refresh, fuentes activas)
│   └── Estado del autotrader (enabled, connected, balance)
│
├── Sub-tab "Señales en Vivo"
│   ├── Tabla de señales actuales (ciudad, rango, edge, confianza, odds)
│   ├── Badge: "ELIMINATION" o "CONVICTION" por tipo
│   ├── Auto-refresh cada 15s
│   └── Botón "Refresh forecasts"
│
├── Sub-tab "Mercados"
│   ├── Cards por ciudad/fecha
│   ├── Dentro de cada card: rangos con probabilidad ensemble vs poly odds
│   ├── Barras de color: verde=edge positivo, rojo=edge negativo
│   └── Temperatura media del ensemble + std dev
│
├── Sub-tab "Paper Trading"
│   ├── KPIs: Capital, PnL total, Win rate, ROI, Trades abiertos
│   ├── Tabla de paper trades abiertos (con PnL en vivo)
│   ├── Historial de trades resueltos
│   ├── Stats por ciudad
│   └── Config: bet size, enabled
│
├── Sub-tab "Auto Trading"
│   ├── Toggle ON/OFF
│   ├── Wallet: usar crypto wallet | wallet separada
│   ├── Balance USDC en vivo
│   ├── Generar API Keys (botón)
│   ├── Config:
│   │   ├── Bankroll, Bet mode (fixed/proportional), Bet size/pct
│   │   ├── Min edge, Min confidence, Min/Max odds
│   │   ├── Max positions, Max daily trades, Max daily loss
│   │   ├── Cooldown, Order type (market/limit)
│   │   ├── Stop Loss (enabled, %), Take Profit (%)
│   │   ├── Trailing Stop (enabled, %)
│   │   ├── Max holding time
│   │   └── Ciudades habilitadas (checkboxes)
│   ├── Posiciones abiertas (con PnL unrealized)
│   ├── Historial de trades reales
│   └── PnL diario gráfico (Chart.js)
│
├── Sub-tab "Configuración"
│   ├── Detector: min edge, min confidence, scan interval
│   ├── Feed: refresh interval, fuentes activas
│   ├── Eliminación: enabled, min profit, max bet
│   ├── Early detector: enabled, pre-market hours
│   ├── Telegram alerts: on/off
│   └── Ciudades a monitorear (tabla con toggle por ciudad)
│
└── Sub-tab "Forecast"
    ├── Tabla de pronósticos por ciudad/fecha
    ├── Ensemble distribution chart (histograma de 31 miembros)
    ├── Comparativa multi-fuente (Open-Meteo vs Weather.gov vs WeatherAPI)
    └── Confianza por fuente
```

---

## 8. VENTAJA COMPETITIVA — Cómo ganarle a otros bots

### 8.1 Multi-Source Consensus (Edge #1)
- La mayoría de bots usan 1 sola API de weather
- Nosotros usamos 3-4 fuentes → mayor confianza → apuestas más grandes

### 8.2 Speed: Pre-Market Analysis (Edge #2)
- Calcular forecasts 12h antes de que abra el mercado
- Cuando abre, ejecutar en <15 segundos
- Otros bots necesitan tiempo para consultar APIs + calcular

### 8.3 Elimination Strategy (Edge #3)
- Comprar NO en rangos imposibles a 95-97¢
- Profit garantizado de 3-5% si forecast es correcto
- La mayoría de bots solo miran YES (convicción)

### 8.4 Dynamic Rebalancing (Edge #4)
- Si forecast cambia durante el día (weather.gov actualiza cada hora)
- Ajustar posiciones: vender si edge se reduce, comprar más si edge aumenta
- HondaCivic hace esto manualmente, nosotros lo automatizamos

### 8.5 Orderbook Intelligence (Edge #5)
- Antes de colocar orden, verificar profundidad del orderbook
- Si hay poca liquidez: usar limit order en vez de market
- Si hay mucha liquidez: market order para velocidad

---

## 9. ARCHIVOS A CREAR / MODIFICAR

### Nuevos archivos
| Archivo | Descripción | ~Líneas |
|---------|-------------|---------|
| `weather_arb/multi_feed.py` | Fuentes adicionales (Weather.gov, WeatherAPI, Visual Crossing) | ~300 |
| `weather_arb/early_weather.py` | Pre-market analysis & early entry detector | ~250 |

### Archivos a modificar
| Archivo | Cambios | ~Líneas cambiadas |
|---------|---------|-------------------|
| `weather_arb/weather_feed.py` | Integrar multi_feed, consensus forecast | +50 |
| `weather_arb/detector.py` | Agregar estrategia eliminación, multi-source | +80 |
| `weather_arb/autotrader.py` | check_risk_management(), sell_position(), wallet sharing | +120 |
| `config.py` | Nuevas constantes WEATHER_* | +30 |
| `main.py` | Loop risk management, early detector weather | +40 |
| `api/routes.py` | Detector config GET/POST, nuevos endpoints | +80 |
| `dashboard/index.html` | Tab Weather completo con 6 subtabs | +800 |
| `storage/database.py` | Nuevas queries weather | +40 |

### Total estimado: ~1,800 líneas nuevas/modificadas

---

## 10. ORDEN DE IMPLEMENTACIÓN (Pasos)

### Fase 1: Backend Core (Primero hacer que funcione bien)
1. **multi_feed.py** — Agregar Weather.gov + WeatherAPI.com
2. **detector.py** — Estrategia eliminación + multi-source
3. **autotrader.py** — Risk management loop + sell_position + wallet sharing
4. **config.py** — Nuevas constantes
5. **main.py** — Integrar loops

### Fase 2: API & Config
6. **routes.py** — Config detector, nuevos endpoints
7. **database.py** — Queries nuevas

### Fase 3: Dashboard
8. **index.html** — Tab Weather completo con subtabs

### Fase 4: Speed & Polish
9. **early_weather.py** — Pre-market detector
10. **Testing** — Paper trading primero, luego real

---

## 11. DEPENDENCIAS NUEVAS

```
# Ya instaladas (no se necesitan nuevas):
httpx       # Para APIs weather (ya está)
asyncio     # Ya está
structlog   # Ya está

# API keys necesarias (gratis):
WEATHERAPI_KEY=xxx      # weatherapi.com (gratis 1M calls/mes)
VISUAL_CROSSING_KEY=xxx # visualcrossing.com (gratis 1000 calls/día)
# Weather.gov: NO necesita API key
```

---

## 12. CONFIG WEATHER vs CRYPTO — Tabla Comparativa

| Config | Crypto Arb | Weather Arb (propuesto) |
|--------|-----------|------------------------|
| Enabled | `at_enabled` | `wt_enabled` |
| Bet size | `at_bet_size` | `wt_bet_size` |
| Min edge | `at_min_edge` → momentum% | `wt_min_edge` → edge ensemble vs poly |
| Min confidence | `at_min_confidence` | `wt_min_confidence` |
| Max odds | `at_max_odds` | `wt_max_odds` |
| Max positions | `at_max_positions` | `wt_max_positions` |
| Order type | `at_order_type` (market/GTC) | `wt_order_type` |
| Stop loss | `at_stop_loss_pct` | `wt_stop_loss_pct` |
| Take profit | `at_take_profit_pct` | `wt_take_profit_pct` |
| Trailing stop | `at_trailing_stop_pct` | `wt_trailing_stop_pct` (**NUEVO**) |
| Max holding | `at_max_holding_sec` | `wt_max_holding_sec` |
| Cooldown | `at_cooldown_sec` | `wt_cooldown_sec` |
| Max daily trades | `at_max_daily_trades` | `wt_max_daily_trades` |
| Max daily loss | `at_max_daily_loss` | `wt_max_daily_loss` |
| Bankroll | — | `wt_bankroll` |
| Bet mode | — | `wt_bet_mode` (fixed/proportional) |
| API Key | `at_api_key` | `wt_api_key` o `at_api_key` (shared) |
| **Wallet sharing** | — | `wt_use_crypto_wallet` (**NUEVO**) |
| **Eliminación** | — | `wt_elimination_enabled` (**NUEVO**) |
| **Multi-source** | — | `wt_multi_source_enabled` (**NUEVO**) |
| **Early detector** | `at_early_entry_enabled` | `wt_early_enabled` (**NUEVO**) |
| **Ciudades** | coins (BTC, ETH, SOL) | `wt_cities` (nyc, london, etc.) |

---

## RESUMEN

Este plan convierte el weather arb de un "detector básico" a un **bot de trading completo** al mismo nivel que el crypto arb, con:

1. ✅ **4 fuentes de datos** weather (vs 1 actual)
2. ✅ **2 estrategias** (convicción + eliminación)
3. ✅ **Paper trading** con PnL en vivo
4. ✅ **Auto trading real** con wallet compartida
5. ✅ **Risk management** activo (TP/SL/trailing cada 30s)
6. ✅ **Pre-market analysis** para velocidad
7. ✅ **Dashboard completo** con 6 subtabs
8. ✅ **Toda la config** editable desde el dashboard

**Estimación de implementación:** ~1,800 líneas en 10 pasos.

---

*¿Apruebas el plan? Si sí, empiezo por Fase 1 (backend core).*
