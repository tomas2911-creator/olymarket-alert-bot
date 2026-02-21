# PLAN DE IMPLEMENTACIÓN: Weather Arb + Sports Odds Bot

## Fecha: 2025-02-21
## Capital objetivo: $500
## Módulos: Weather Arb (activar/mejorar) + Sports Odds (nuevo)
## ENFOQUE: 100% POLYMARKET — No operamos en ninguna otra plataforma
## Datos externos (Open-Meteo, Pinnacle, etc) = solo fuente de información, NO operamos ahí

---

# PARTE 0: INVESTIGACIÓN GITHUB — BOTS REALES QUE HACEN ESTO

## Weather Trading Bots encontrados:

### 1. suislanchez/polymarket-kalshi-weather-bot ⭐ (el más completo)
- **Stack**: FastAPI + React + SQLite
- **Estrategia**: Ensemble 31 miembros GFS via Open-Meteo → probabilidades por rango → edge vs Polymarket odds
- **Sizing**: Quarter-Kelly (kelly_fraction * 0.25), cap 5% bankroll
- **Threshold**: edge > 8% para señal
- **Fuentes**: Open-Meteo (gratis, sin API key), NWS API
- **Lo útil**: La lógica de edge detection y Kelly sizing — ya lo tenemos implementado

### 2. d3CryptoDreamer/polymarket-sports-trading-bot
- **Stack**: Bun + TypeScript
- **Estrategia**: Compra YES cuando midpoint < 0.5 − edge en mercados deportivos
- **Filtro**: Por sport tag via Gamma API
- **Ejecución**: Limit orders, auto-redeem, approve allowance
- **Lo que tiene que TÚ NO**: Filtrado por tag deportivo, auto-redeem integrado

### 3. carterlasalle/SportsArbFinder
- **Stack**: Python + The-Odds-API
- **Estrategia**: LEE odds de sportsbooks para calcular probabilidades reales
- **Lo útil para nosotros**: La integración con The-Odds-API para LEER datos (no operamos ahí, solo leemos)

---

# PARTE 1: ANÁLISIS COMPARATIVO — Crypto Arb AutoTrader vs Weather/Sports

## Funciones del Crypto Arb AutoTrader (1,662 líneas — TU MEJOR MÓDULO)

He leído las 1,662 líneas completas. Estas son TODAS las funciones que hacen que funcione:

| # | Función | Crypto Arb | Weather Arb | Sports (nuevo) |
|---|---------|-----------|-------------|----------------|
| 1 | **initialize()** — cargar config DB + crear CLOB client | ✅ 82 config keys | ✅ Tiene (26 keys) | ❌ No existe |
| 2 | **_init_clob_client()** — ClobClient con ApiCreds | ✅ Completo | ✅ Tiene | ❌ No existe |
| 3 | **_setup_allowances()** — verificar token allowances | ✅ Completo | ❌ NO TIENE | ❌ No existe |
| 4 | **reload_config()** — hot reload desde dashboard | ✅ | ✅ Tiene | ❌ No existe |
| 5 | **reset_state()** — limpiar posiciones/trades | ✅ | ✅ Tiene | ❌ No existe |
| 6 | **_load_today_trades()** — cargar desde DB | ✅ | ✅ Tiene | ❌ No existe |
| 7 | **_load_open_positions()** — posiciones abiertas | ✅ | ✅ Tiene | ❌ No existe |
| 8 | **evaluate_signal()** — filtros de entrada | ✅ 17 filtros | ✅ 12 filtros | ❌ No existe |
| 9 | **execute_trade()** — colocar orden CLOB | ✅ FOK+GTC+Maker+Hybrid | ✅ FOK+GTC | ❌ No existe |
| 10 | **_get_token_id()** — resolver token del outcome | ✅ Up/Down/Yes/No | ✅ Solo Yes | ❌ No existe |
| 11 | **_get_best_ask()** — live price del CLOB | ✅ | ❌ NO TIENE | ❌ No existe |
| 12 | **_execute_maker_order()** — orden post-only | ✅ | ❌ NO TIENE | ❌ No existe |
| 13 | **_execute_hybrid_order()** — maker con sesgo | ✅ | ❌ NO TIENE | ❌ No existe |
| 14 | **manage_maker_orders()** — gestión de órdenes abiertas | ✅ | ❌ NO TIENE | ❌ No existe |
| 15 | **process_signals()** — loop principal con lock | ✅ | ✅ Tiene | ❌ No existe |
| 16 | **check_risk_management()** — SL/TP/trailing/max hold | ✅ 4 tipos | ✅ Parcial (SL/TP) | ❌ No existe |
| 17 | **_sell_position()** — SELL FOK para cerrar | ✅ | ❌ NO TIENE | ❌ No existe |
| 18 | **resolve_trades()** — resolver mercados cerrados | ✅ CLOB+Gamma+Stale | ✅ CLOB+Gamma | ❌ No existe |
| 19 | **get_status()** — estado para dashboard | ✅ | ✅ Tiene | ❌ No existe |
| 20 | **get_usdc_balance()** — saldo on-chain | ✅ Multi-RPC+CLOB | ✅ Básico | ❌ No existe |
| 21 | **test_connection()** — verificar credenciales | ✅ | ✅ Tiene | ❌ No existe |
| 22 | **auto_claim()** — redeem posiciones ganadoras | ✅ Builder Relayer | ❌ NO TIENE | ❌ No existe |
| 23 | **Bankroll tracking** — gestión de capital | ✅ Via BankrollTracker | ✅ Interno (basic) | ❌ No existe |

## Gaps críticos del Weather Arb actual vs Crypto Arb:

1. **❌ _setup_allowances()** — No verifica que la wallet tenga permiso para operar
2. **❌ _get_best_ask()** — No consulta precio live, usa precio stale de Gamma
3. **❌ _sell_position()** — NO PUEDE CERRAR POSICIONES ANTICIPADAMENTE
4. **❌ auto_claim()** — No reclama ganancias automáticamente
5. **❌ Maker orders** — Solo FOK/GTC, sin maker (pierde rebate en mercados weather que NO cobran fee)
6. **❌ Trailing stop** — Solo tiene SL/TP básico, sin trailing

---

# PARTE 2: PLAN DETALLADO — WEATHER ARB

## Estado actual: 80% implementado, necesita 6 mejoras críticas

### FASE W1: Activar y verificar que funcione (antes de tocar código)

**W1.1 — Habilitar feature flag**
- Archivo: `config.yaml`
- Cambio: `weather_arb: true` en features
- Verificar que `FEATURE_WEATHER_ARB` se active

**W1.2 — Configurar credenciales weather wallet**
- Las credenciales weather son SEPARADAS (prefijo `wt_`)
- Necesita: `wt_api_key`, `wt_api_secret`, `wt_private_key`, `wt_passphrase`, `wt_funder_address`
- Se configuran desde el dashboard o directamente en DB
- OPCIÓN: Reusar las MISMAS credenciales del crypto arb (misma wallet), solo cambia el prefijo en DB

**W1.3 — Verificar que Open-Meteo responde**
- URL: `https://ensemble-api.open-meteo.com/v1/ensemble`
- Parámetros: lat, lon, hourly=temperature_2m, models=gfs_seamless
- Sin API key necesaria
- Test: curl manual para verificar

**W1.4 — Verificar que Gamma devuelve mercados weather**
- Slug pattern: `highest-temperature-in-{city}-on-{month}-{day}-{year}`
- Test: buscar slug de hoy para New York
- Las 14 ciudades ya están configuradas en `weather_feed.py`

**W1.5 — Ejecutar en modo paper primero**
- El `WeatherPaperTrader` ya funciona independientemente del autotrader
- Dejar corriendo 2-3 días para validar señales
- Verificar win rate del paper antes de activar autotrading real

### FASE W2: Mejoras críticas al Weather AutoTrader

**W2.1 — Agregar _setup_allowances() (copiar de crypto arb)**
- Copiar lógica de `crypto_arb/autotrader.py` líneas 208-235
- Adaptar para weather autotrader
- Llamar desde `_init_clob_client()` después de crear el cliente
- Esto PREVIENE el error "order failed" por falta de allowance

**W2.2 — Agregar _get_best_ask() para precio live**
- Copiar de `crypto_arb/autotrader.py` líneas 707-727
- Usar en `execute_trade()` antes de colocar orden
- Recalcular edge con precio live (como hace sniper en crypto)
- Esto evita comprar a precio stale cuando el mercado ya se movió

**W2.3 — Agregar _sell_position() para cierre anticipado**
- Copiar patrón de `crypto_arb/autotrader.py` líneas 1203-1261
- Crear orden SELL FOK con slippage negativo
- Conectar a `check_risk_management()` que ya tiene SL/TP
- Esto permite salir de posiciones perdedoras ANTES de que el mercado cierre

**W2.4 — Agregar trailing stop**
- Copiar lógica de `crypto_arb/autotrader.py` líneas 1186-1192
- Agregar `_trailing_highs: dict[str, float]` al estado
- Agregar config keys: `wt_trailing_stop_enabled`, `wt_trailing_stop_pct`
- Actualizar `check_risk_management()` con el check de trailing

**W2.5 — Agregar auto_claim() (redeem automático)**
- Copiar de `crypto_arb/autotrader.py` líneas 1569-1661
- Builder Relayer para reclamar posiciones ganadoras
- Llamar cada 2 min desde el loop de weather
- Esto recupera USDC automáticamente después de ganar

**W2.6 — Agregar soporte Maker Orders (0% fee en weather)**
- Los mercados weather en Polymarket NO cobran taker fee
- Pero maker orders aún tienen ventaja: mejor precio + rebate
- Copiar `_execute_maker_order()` de crypto arb
- Agregar config: `wt_order_type: maker` como opción
- Esto permite entrar a mejor precio en mercados con spread

### FASE W3: Mejoras al detector y feed

**W3.1 — Múltiples fuentes de forecast (redundancia)**
- Actualmente solo usa Open-Meteo ensemble (GFS)
- Agregar: NWS API (National Weather Service) como segundo modelo
  - URL: `https://api.weather.gov/gridpoints/{office}/{x},{y}/forecast`
  - Gratis, sin API key
- Calcular: probabilidad promedio ponderada entre GFS y NWS
- Esto AUMENTA la confianza de las señales

**W3.2 — Detector de mercados con slug alternativo**
- Actualmente busca: `highest-temperature-in-{city}-on-{month}-{day}-{year}`
- Polymarket puede cambiar el formato de slugs
- Agregar búsqueda alternativa: `events?tag=weather` como fallback
- Esto evita que el bot se rompa si cambian los slugs

**W3.3 — Filtro de liquidez**
- Actualmente no verifica si hay liquidez en el order book
- Agregar: consultar `/book` antes de comprar
- Skip si no hay asks dentro del 5% del precio indicativo
- Esto evita trades en mercados sin liquidez (spread enorme)

**W3.4 — Configuración de ciudades por rentabilidad**
- Analizar paper trading data para ver qué ciudades dan mejor win rate
- Permitir deshabilitar ciudades con bajo rendimiento
- Config: `wt_cities: new-york,chicago,los-angeles` (solo las rentables)

### FASE W4: Dashboard weather (ya parcialmente existe)

**W4.1 — Verificar rutas API existentes**
- `/api/weather/status` — estado general
- `/api/weather/signals` — señales activas
- `/api/weather/markets` — mercados activos con rangos
- `/api/weather/paper` — paper trading stats
- `/api/weather/config` — GET/POST config del autotrader

**W4.2 — Agregar panel de configuración weather al dashboard**
- Sección en HTML con inputs para: bankroll, bet_size, min_edge, min_confidence, cities
- Botón de test connection (como crypto arb)
- Mostrar balance USDC de la wallet weather
- Mostrar paper trading PnL en tiempo real

### FASE W5: Base de datos

**W5.1 — Verificar tablas weather existentes**
- `weather_trades` — trades reales ejecutados
- `weather_config` — configuración del autotrader
- Si no existen, crearlas con migración

**W5.2 — Métodos DB requeridos** (verificar que existen):
- `record_weather_trade(trade_record, user_id)`
- `resolve_weather_trade(cid, result, pnl, user_id)`
- `get_weather_trades(hours, user_id)`
- `get_open_weather_trades(user_id)`

---

# PARTE 3: PLAN DETALLADO — SPORTS ODDS BOT (NUEVO)

## Estado actual: 0% implementado, se construye desde cero

### Concepto (100% POLYMARKET — sportsbooks son solo FUENTE DE DATOS):
LEER odds de sportsbooks (Pinnacle, DraftKings) para saber la probabilidad REAL de un evento.
Comparar esa probabilidad real con el precio en Polymarket.
Si Polymarket subestima → comprar en Polymarket.

**NO operamos en ningún sportsbook. NO necesitamos cuenta en Pinnacle.**
**Solo LEEMOS sus odds (gratis via The-Odds-API) como fuente de información.**
**Es como mirar el termómetro antes de apostar sobre el clima.**

### FASE S1: Estructura del módulo (nuevo directorio)

```
src/sports_arb/
├── __init__.py
├── odds_feed.py        # Obtener odds de The-Odds-API
├── market_matcher.py   # Emparejar mercados Polymarket ↔ sportsbooks
├── detector.py         # Detectar edges (odds vs Polymarket)
├── autotrader.py       # Ejecutar trades (copiar patrón de crypto arb)
└── backtester.py       # Paper trading para validar
```

### FASE S2: The-Odds-API Feed (`odds_feed.py`)

**S2.1 — Integrar The-Odds-API**
- URL: `https://api.the-odds-api.com/v4/sports/{sport}/odds`
- API Key: GRATIS (500 requests/mes) → suficiente para empezar
- Parámetros: `regions=us,eu`, `markets=h2h,spreads,totals`, `oddsFormat=decimal`
- Deportes: NFL, NBA, MLB, NHL, MMA, Soccer (los que Polymarket ofrece)
- Rate: 1 request cada 5 min por deporte = ~8640 req/mes con 6 deportes = necesita plan básico ($20/mes)
- ALTERNATIVA FREE: Odds API tier gratis = 500 req/mes → polling cada 30 min para 2 deportes

**S2.2 — Estructura del OddsFeed**
```python
class OddsFeed:
    """Obtiene odds de múltiples sportsbooks via The-Odds-API."""
    def __init__(self, api_key: str, sports: list[str], refresh_interval: int = 300):
        ...
    async def start(self):
        """Loop de refresh periódico."""
    async def _refresh_all(self):
        """Actualizar odds para todos los deportes."""
    def get_odds(self, sport: str, event_name: str) -> dict:
        """Obtener odds para un evento específico."""
    def get_consensus_probability(self, sport: str, event_name: str, outcome: str) -> float:
        """Calcular probabilidad implícita promedio de múltiples sportsbooks."""
```

**S2.3 — Conversión odds → probabilidad implícita**
- Odds decimal: prob = 1 / odds
- Ejemplo: odds 2.10 → prob = 1/2.10 = 47.6%
- IMPORTANTE: Eliminar vig/juice del sportsbook
  - Total implied prob > 100% (ej: 52% + 52% = 104%)
  - Normalizar: prob_real = prob_raw / sum(all_probs)
  - Usar Pinnacle como "sharp" benchmark (odds más eficientes)

**S2.4 — Sportsbooks a usar**
- **Pinnacle** (prioridad máxima): Las odds más sharp del mundo, benchmark de la industria
- **DraftKings**: Mercado US, buena liquidez
- **FanDuel**: Mercado US
- **Bet365**: Mercado global
- Consensus = promedio ponderado (Pinnacle peso 2x, otros peso 1x)

### FASE S3: Market Matcher (`market_matcher.py`)

**S3.1 — Buscar mercados deportivos en Polymarket**
- Usar Gamma API: `events?tag=sports` o `events?tag=nfl`, `events?tag=nba`, etc.
- Filtrar: solo mercados activos, no cerrados, con liquidez
- Extraer: equipos, liga, tipo de apuesta (moneyline, spread, total)

**S3.2 — Matching entre Polymarket y sportsbooks**
```python
class MarketMatcher:
    """Emparejar mercados Polymarket con eventos de sportsbooks."""
    def match_markets(self, poly_markets: list, odds_data: dict) -> list[MatchedMarket]:
        """Encontrar pares equivalentes."""
    def _normalize_team_name(self, name: str) -> str:
        """Normalizar nombres de equipos para matching."""
    def _compute_similarity(self, poly_question: str, odds_event: str) -> float:
        """Text similarity para emparejar preguntas."""
```

**S3.3 — Tipos de matching**
- **Moneyline**: "Will Lakers win?" ↔ Lakers moneyline odds
- **Spread**: "Will Lakers cover -3.5?" ↔ Lakers spread odds
- **Totals**: "Will game go over 220.5?" ↔ Over/Under odds
- **Props**: "Will LeBron score 25+?" → más complejo, fase futura

**S3.4 — Diccionario de nombres de equipos**
- Polymarket puede usar: "Los Angeles Lakers", "Lakers", "LAL"
- The-Odds-API usa formato estándar
- Crear mapping estático + fuzzy matching como backup

### FASE S4: Detector (`detector.py`)

**S4.1 — Estructura del SportsArbDetector**
```python
class SportsArbDetector:
    """Detecta oportunidades de arbitraje entre Polymarket y sportsbooks."""
    def __init__(self, odds_feed: OddsFeed, market_matcher: MarketMatcher):
        ...
    async def start(self):
        """Loop principal de detección."""
    async def _scan_active_markets(self):
        """Buscar mercados deportivos activos en Polymarket."""
    async def _check_edges(self) -> list[SportsSignal]:
        """Comparar probabilidades implícitas vs Polymarket odds."""
```

**S4.2 — Cálculo del edge**
```
consensus_prob = promedio ponderado de sportsbooks (Pinnacle 2x)
poly_odds = precio YES en Polymarket
edge_pct = (consensus_prob - poly_odds) * 100

Si edge_pct > MIN_EDGE (ej: 5%) → SEÑAL
```

**S4.3 — Filtros de señal** (copiar patrón de crypto arb evaluate_signal)
- Edge mínimo (configurable, default 5%)
- Confianza mínima (consensus de N+ sportsbooks)
- Max odds (no comprar si ya muy caro)
- Min liquidez en Polymarket
- Cooldown entre trades
- Max posiciones abiertas
- Max pérdida diaria
- No duplicar posición en mismo evento
- Blacklist de señales fallidas

**S4.4 — Ventaja específica de deportes**
- Los mercados deportivos en Polymarket tienen **0% fees** (como weather)
- Los sportsbooks cobran vig/juice (~4-5%)
- Si Polymarket ofrece odds peores que Pinnacle → edge real
- Ejemplo: Pinnacle da Lakers 55% prob, Polymarket YES = $0.48 → edge = 7%

### FASE S5: AutoTrader (`autotrader.py`)

**S5.1 — Copiar estructura completa de crypto arb autotrader**
- TODAS las 23 funciones listadas en la tabla de arriba
- Adaptar prefijo config: `sp_` (sports)
- Wallet SEPARADA o compartida (configurable)

**S5.2 — Config keys del Sports AutoTrader**
```
sp_enabled, sp_bet_size, sp_min_edge, sp_min_confidence,
sp_max_odds, sp_max_positions, sp_order_type,
sp_max_daily_loss, sp_max_daily_trades, sp_cooldown_sec,
sp_sports (NFL,NBA,MLB,etc),
sp_api_key, sp_api_secret, sp_private_key, sp_passphrase,
sp_funder_address,
sp_bankroll, sp_bet_mode, sp_bet_pct,
sp_stop_loss_enabled, sp_stop_loss_pct,
sp_take_profit_pct, sp_max_holding_sec,
sp_trailing_stop_enabled, sp_trailing_stop_pct,
sp_odds_api_key (The-Odds-API key)
```

**S5.3 — Funciones que se copian DIRECTAMENTE de crypto arb**
- `_init_clob_client()` — idéntico
- `_setup_allowances()` — idéntico
- `execute_trade()` — adaptar: token_id = Yes (deportes son Yes/No)
- `_sell_position()` — idéntico
- `check_risk_management()` — idéntico (SL/TP/trailing/max hold)
- `resolve_trades()` — adaptar: resolución por resultado del partido
- `auto_claim()` — idéntico
- `get_usdc_balance()` — idéntico
- `test_connection()` — idéntico

**S5.4 — Resolución de trades deportivos**
- Los mercados deportivos se resuelven cuando el partido termina
- Polling Gamma API cada 5 min para verificar resolución
- Fallback: The-Odds-API tiene scores en vivo para verificar
- Tiempo de hold: horas a días (no minutos como crypto)

### FASE S6: Paper Trading (`backtester.py`)

**S6.1 — Copiar patrón de WeatherPaperTrader**
- Registrar cada señal como paper trade
- Resolver cuando el mercado cierra en Polymarket
- Calcular PnL basado en resolución real
- Estadísticas por deporte

### FASE S7: Integración en main.py

**S7.1 — Imports**
```python
from src.sports_arb.odds_feed import OddsFeed
from src.sports_arb.market_matcher import MarketMatcher
from src.sports_arb.detector import SportsArbDetector
from src.sports_arb.autotrader import SportsAutoTrader
from src.sports_arb.backtester import SportsPaperTrader
```

**S7.2 — Estado en PolymarketAlertBot**
```python
# Sports Arb
self.odds_feed = None
self.sports_matcher = None
self.sports_detector = None
self.sports_autotrader = None
self.sports_paper = None
```

**S7.3 — Método _start_sports_arb()** (copiar patrón de _start_weather_arb)
```python
async def _start_sports_arb(self):
    odds_api_key = config.SPORTS_ODDS_API_KEY
    sports = config.SPORTS_ARB_SPORTS
    self.odds_feed = OddsFeed(api_key=odds_api_key, sports=sports)
    self.sports_matcher = MarketMatcher()
    self.sports_detector = SportsArbDetector(self.odds_feed, self.sports_matcher)
    self.sports_autotrader = SportsAutoTrader(self.db)
    await self.sports_autotrader.initialize()
    self.sports_paper = SportsPaperTrader()
    # Lanzar loops
    asyncio.create_task(self._run_odds_feed())
    asyncio.create_task(self._run_sports_detector())
    asyncio.create_task(self._run_sports_signal_loop())
```

**S7.4 — Loops de background** (copiar patrón de weather)
- `_run_odds_feed()` — actualizar odds cada 5 min
- `_run_sports_detector()` — buscar mercados cada 5 min
- `_run_sports_signal_loop()` — evaluar señales + paper + autotrading cada 30s

### FASE S8: Config

**S8.1 — Nuevas variables en config.py**
```python
# === Sports Arb ===
_sports = _yaml.get("sports_arb", {})
FEATURE_SPORTS_ARB = _features.get("sports_arb", False)
SPORTS_ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
SPORTS_ARB_SPORTS = _sports.get("sports", ["americanfootball_nfl", "basketball_nba"])
SPORTS_ARB_MIN_EDGE = _sports.get("min_edge_pct", 5.0)
SPORTS_ARB_MIN_CONFIDENCE = _sports.get("min_confidence_pct", 60.0)
SPORTS_ARB_MAX_POLY_ODDS = _sports.get("max_poly_odds", 0.85)
SPORTS_ARB_SCAN_INTERVAL = _sports.get("scan_interval_sec", 300)
SPORTS_ARB_ODDS_REFRESH = _sports.get("odds_refresh_sec", 300)
SPORTS_ARB_PAPER_BET = _sports.get("paper_bet_size", 50)
```

**S8.2 — config.yaml sección nueva**
```yaml
features:
  sports_arb: false  # Habilitar cuando esté listo

sports_arb:
  sports:
    - americanfootball_nfl
    - basketball_nba
    - baseball_mlb
    - icehockey_nhl
    - mma_mixed_martial_arts
  min_edge_pct: 5.0
  min_confidence_pct: 60.0
  max_poly_odds: 0.85
  scan_interval_sec: 300
  odds_refresh_sec: 300
  paper_bet_size: 50
```

### FASE S9: Dashboard sports

**S9.1 — API routes nuevas**
- `/api/sports/status` — estado general
- `/api/sports/signals` — señales activas
- `/api/sports/markets` — mercados emparejados
- `/api/sports/paper` — paper trading stats
- `/api/sports/config` — GET/POST config
- `/api/sports/odds` — odds actuales por deporte

### FASE S10: Base de datos

**S10.1 — Tablas nuevas**
- `sports_trades` — trades reales ejecutados
- `sports_signals` — señales históricas

**S10.2 — Métodos DB nuevos**
- `record_sports_trade(trade_record, user_id)`
- `resolve_sports_trade(cid, result, pnl, user_id)`
- `get_sports_trades(hours, user_id)`
- `get_open_sports_trades(user_id)`

---

# PARTE 4: ORDEN DE EJECUCIÓN

## Prioridad 1: Weather Arb (ya está 80% implementado)

```
Semana 1, Día 1-2:
  W1.1 → Habilitar feature flag en config.yaml
  W1.2 → Configurar credenciales (reusar wallet crypto si es necesario)
  W1.3 → Test manual: Open-Meteo responde
  W1.4 → Test manual: Gamma devuelve mercados weather
  W1.5 → Activar y dejar en paper mode

Semana 1, Día 3-4:
  W2.1 → Agregar _setup_allowances()
  W2.2 → Agregar _get_best_ask() + recalcular edge con precio live
  W2.3 → Agregar _sell_position() para cierre anticipado
  W2.4 → Agregar trailing stop
  W5.1-W5.2 → Verificar tablas y métodos DB

Semana 1, Día 5:
  W2.5 → Agregar auto_claim()
  W2.6 → Agregar maker orders (weather = 0% fee)
  W4.2 → Panel de config en dashboard

Semana 2, Día 1-3:
  W3.1 → Agregar NWS API como segunda fuente
  W3.2 → Fallback de búsqueda por tag
  W3.3 → Filtro de liquidez
  Validar paper trading results
  Activar autotrading real con $50 iniciales si paper es positivo
```

## Prioridad 2: Sports Odds Bot (construcción nueva)

```
Semana 2, Día 4-5:
  S1 → Crear estructura src/sports_arb/
  S2 → Implementar OddsFeed con The-Odds-API
  S8 → Config + feature flag

Semana 3, Día 1-2:
  S3 → Implementar MarketMatcher (team names + fuzzy matching)
  S4 → Implementar SportsArbDetector

Semana 3, Día 3-4:
  S5 → Implementar SportsAutoTrader (copiar de crypto arb)
  S6 → Implementar SportsPaperTrader
  S10 → Tablas DB + métodos

Semana 3, Día 5:
  S7 → Integrar en main.py
  S9 → API routes + dashboard
  Activar en paper mode

Semana 4:
  Validar paper trading
  Ajustar min_edge, filtros
  Activar autotrading real con $50
```

---

# PARTE 5: RIESGOS Y MITIGACIONES

| Riesgo | Probabilidad | Impacto | Mitigación |
|--------|-------------|---------|------------|
| Open-Meteo ensemble incorrecto | Media | Alto | Agregar NWS como segunda fuente, paper test primero |
| No hay mercados weather activos | Baja | Alto | Los hay todos los días para 14 ciudades |
| The-Odds-API rate limit | Media | Medio | Polling cada 5-10 min, cache agresivo |
| Matching incorrecto poly↔sportsbook | Alta | Alto | Validación manual inicial, diccionario de nombres |
| Falta liquidez en Polymarket sports | Media | Alto | Filtro de liquidez + min volume |
| Wallet sin allowance | Alta | Alto | _setup_allowances() + instrucciones en dashboard |
| Polymarket cambia slugs weather | Baja | Medio | Fallback por tag search |

---

# PARTE 6: ESTIMACIÓN DE RENTABILIDAD

## Weather Arb
- **Edge promedio encontrado**: 8-15% (según repo suislanchez)
- **Win rate estimado**: 55-65% (ensemble es bastante preciso)
- **Fees**: 0% en weather markets
- **Capital recomendado**: $100-200
- **Trades/día**: 3-8
- **ROI estimado**: 5-15% mensual si win rate >58%

## Sports Odds
- **Edge promedio esperado**: 3-8% (Pinnacle es muy eficiente)
- **Win rate estimado**: 52-58% (odds de sportsbooks son sharp)
- **Fees**: 0% en sports markets Polymarket
- **Capital recomendado**: $200-300
- **Trades/día**: 1-5 (depende de calendario deportivo)
- **ROI estimado**: 3-10% mensual si edge >5%

## Distribución $500
- Weather Arb: $200 (más predecible, edge más claro)
- Sports Odds: $200 (diversificación)
- Reserva: $100 (para cubrir drawdowns)

---

# RESUMEN EJECUTIVO

**Weather Arb**: Ya tienes el 80%. Faltan 6 funciones que se copian directamente del crypto arb.
Se puede tener funcionando en 3-5 días de trabajo.

**Sports Odds**: Módulo nuevo, pero el 50% del código se copia del crypto arb (autotrader, risk management,
CLOB integration). La parte nueva es The-Odds-API + market matching. Se puede tener en 5-7 días.

**Total estimado**: 2-3 semanas para ambos módulos funcionando con trades reales.

¿APRUEBAS ESTE PLAN PARA COMENZAR LA EJECUCIÓN?
