# Plan de Implementación: Crypto Market Maker (Hybrid Maker-Directional)

## Objetivo
Implementar el componente de **market making real** que falta en la estrategia "Hybrid Maker-Directional".
Cuando NO hay señal direccional clara, el bot coloca órdenes a **ambos lados** del mercado
(BUY "Up" + BUY "Down") para capturar el spread + rebates de maker.

---

## Estado Actual del Código

### Lo que YA existe:
- **`src/strategies/market_maker.py`** → `MarketMakerBot` (105 líneas) — esqueleto básico, solo paper/log, NO ejecuta en CLOB real
- **`src/crypto_arb/paper_trader.py`** → `MakerPaperTrader` (431 líneas) — paper trading DIRECCIONAL (1 lado), NO bilateral
- **`src/crypto_arb/autotrader.py`** → `AutoTrader` — ejecuta trades reales pero SOLO direccionales (1 lado por señal)
- **`src/crypto_arb/detector.py`** → `CryptoArbDetector` — detecta mercados crypto up/down activos por slug
- **`src/crypto_arb/binance_feed.py`** → `BinanceFeed` — precio spot en tiempo real
- **`src/config.py`** → constantes `MM_*` y `FEATURE_MARKET_MAKING` (líneas 326-332)
- **`src/main.py`** → `MarketMakerBot` importado (línea 32), inicializado en `_start_v8_modules` (línea 198), tick cada 2 ciclos (línea 607)

### Lo que FALTA:
1. Lógica de quoting bilateral (BUY Up + BUY Down simultáneo)
2. Gestión de inventario (cuánto exposure tenemos por lado)
3. Re-quoting dinámico (ajustar precios cada 5-10s según orderbook)
4. Integración con CLOB real (py-clob-client)
5. Sesgo direccional basado en señales del detector
6. Config persistente en DB + API routes
7. Dashboard UI sub-tab

---

## Paso 1: Reescribir `src/strategies/market_maker.py`

### Clase: `CryptoMarketMaker`

Reemplazar completamente el `MarketMakerBot` actual (105 líneas) con ~400 líneas.

**Constructor `__init__(self, db, binance_feed, detector)`:**
```
- self.db = db
- self._feed = binance_feed           # Para precio spot
- self._detector = detector            # Para señales direccionales
- self._client = None                  # py-clob-client ClobClient (se inicializa en initialize())
- self._enabled = False
- self._initialized = False
- self._config = {}                    # Cargado desde DB
- self._active_pairs: dict = {}        # condition_id -> {up_order_id, down_order_id, up_token_id, down_token_id, ...}
- self._inventory: dict = {}           # condition_id -> {up_shares, down_shares, up_cost, down_cost}
- self._open_orders: dict = {}         # order_id -> {cid, side, outcome, price, shares, placed_ts}
- self._stats = {pnl, trades, fills, rebates, requotes, ...}
- self._last_quote_time: dict = {}     # cid -> timestamp última cotización
- self._markets_cache: list = []       # Mercados elegibles
- self._markets_cache_ts = 0
```

**Método `async initialize()`:**
```
1. Cargar config desde DB (tabla bot_config):
   - mm_enabled (bool)
   - mm_bet_size (float, default 5) — tamaño por lado
   - mm_spread_target (float, default 0.04) — spread objetivo (4 centavos)
   - mm_max_inventory (float, default 50) — máx exposure neto por mercado
   - mm_max_markets (int, default 3) — máx mercados simultáneos
   - mm_requote_sec (int, default 10) — re-quotear cada N segundos
   - mm_requote_threshold (float, default 0.02) — re-quotear si precio cambia > X
   - mm_fill_timeout_sec (int, default 120) — cancelar orden si no llena
   - mm_bias_enabled (bool) — usar señales del detector para sesgar
   - mm_bias_strength (float, default 0.02) — cuánto sesgar (centavos)
   - mm_paper_mode (bool, default True) — empezar en paper
   - mm_max_daily_loss (float, default 20) — pausar si pierde > X
   - mm_min_time_remaining_sec (int, default 180) — no quotear mercados que cierran pronto
2. Inicializar py-clob-client (reusar credenciales del AutoTrader existente)
   - Leer POLY_PRIVATE_KEY, POLY_API_KEY, etc. de env
   - Si no hay wallet → solo paper mode
3. Marcar _initialized = True
```

**Método `async tick()` — llamado cada 5s desde main loop:**
```
1. Si no _enabled o no _initialized → return
2. Refrescar mercados elegibles (cada 60s):
   - Usar self._detector._active_markets para obtener mercados crypto up/down activos
   - Filtrar: time_remaining > mm_min_time_remaining_sec
   - Filtrar: tiene tokens Up y Down con precios entre 0.10 y 0.90
   - Ordenar por liquidez/volumen
   - Tomar los top mm_max_markets
3. Para cada mercado elegible:
   a. Obtener precios actuales de Up y Down del CLOB
   b. Calcular sesgo direccional (si mm_bias_enabled):
      - Consultar señales recientes del detector para este mercado
      - Si hay señal fuerte "up" → bid más agresivo en Up, más defensivo en Down
      - Si no hay señal → simétrico
   c. Calcular precios de quote:
      - Sin sesgo: bid_up = mid - spread/2, bid_down = mid - spread/2
      - Con sesgo up: bid_up = mid - spread/2 + bias, bid_down = mid - spread/2 - bias
   d. Verificar inventario: si ya tenemos mucho de un lado → no quotear ese lado
   e. Colocar/actualizar órdenes (requote si precio cambió > threshold)
4. Gestionar órdenes abiertas:
   - Polling cada orden: verificar status (matched/cancelled/expired)
   - Si matched → actualizar inventario, registrar fill
   - Si timeout → cancelar
5. Verificar pares completados:
   - Si tenemos shares de Up Y Down en el mismo mercado → profit = sum - cost
   - Si mercado resuelve → calcular PnL real
6. Check max_daily_loss → pausar si excedido
```

**Método `async _place_bilateral_quote(cid, up_token_id, down_token_id, up_price, down_price, size)`:**
```
1. Calcular shares para cada lado: shares = size / price
2. MIN_CLOB_SHARES = 5 → verificar que alcanza
3. Si paper_mode:
   - Registrar en memoria como paper orders
   - Simular fills basándose en precio actual vs nuestro bid
4. Si real mode:
   - Crear OrderArgs BUY para Up token
   - Crear OrderArgs BUY para Down token
   - Post ambos como GTC (post-only si es posible)
   - Guardar order_ids en _open_orders
5. Log: "[MM] Quote: Up@${up_price} + Down@${down_price} en {market}"
```

**Método `async _manage_open_orders()`:**
```
Para cada orden abierta:
1. Si paper: verificar si precio actual <= nuestro bid → fill simulado
2. Si real: poll CLOB status
3. Si matched:
   - Actualizar _inventory[cid][outcome] += shares
   - Registrar fill en DB (tabla mm_trades)
   - Acumular rebate estimado (0.5% del volumen)
4. Si timeout:
   - Cancelar orden
   - Requotear con precio actualizado
```

**Método `async _check_completed_pairs()`:**
```
Para cada mercado en _inventory:
1. Si tiene shares de Up Y Down:
   - min_shares = min(up_shares, down_shares)
   - profit = min_shares * $1.00 - (up_cost_per_share + down_cost_per_share) * min_shares
   - Si profit > 0 → "spread capturado"
   - Reducir inventario de ambos lados
2. Si mercado resuelve:
   - Shares ganadoras pagan $1 cada una
   - Shares perdedoras pagan $0
   - PnL = winning_shares * 1 - total_cost
```

**Método `async _resolve_market(cid)`:**
```
1. Consultar CLOB: ¿mercado cerrado? ¿cuál ganó?
2. Calcular PnL basado en inventario:
   - Si Up ganó: pnl = up_shares * 1.0 - up_cost - down_cost
   - Si Down ganó: pnl = down_shares * 1.0 - up_cost - down_cost
3. Registrar en DB
4. Limpiar inventario
```

**Método `get_status() -> dict`:**
```
Return {
    enabled, paper_mode, active_markets, open_orders,
    inventory (per market), daily_pnl, total_pnl,
    fills, requotes, total_rebates, avg_spread_captured
}
```

---

## Paso 2: Tabla DB `mm_trades`

Agregar en `database.py` → `_create_tables()`:

```sql
CREATE TABLE IF NOT EXISTS mm_trades (
    id SERIAL PRIMARY KEY,
    condition_id TEXT NOT NULL,
    market_question TEXT,
    outcome TEXT,           -- 'Up' o 'Down'
    side TEXT DEFAULT 'BUY',
    order_id TEXT,
    price REAL,
    shares REAL,
    cost_usdc REAL,
    rebate_est REAL DEFAULT 0,
    status TEXT DEFAULT 'open',    -- open, filled, resolved
    result TEXT,                    -- win, loss, spread_captured
    pnl REAL DEFAULT 0,
    is_paper BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);
```

**Funciones DB nuevas:**
- `async record_mm_trade(data: dict) -> int`
- `async get_mm_trades(hours=24, paper=None) -> list`
- `async resolve_mm_trade(condition_id, outcome_winner, pnl)`
- `async get_mm_stats() -> dict` (PnL, fills, win rate, etc.)

---

## Paso 3: Config en DB

Agregar las siguientes keys al sistema de config existente (tabla `bot_config`, funciones `save_config`/`get_config_bulk`):

```
mm_enabled = false
mm_bet_size = 5
mm_spread_target = 0.04
mm_max_inventory = 50
mm_max_markets = 3
mm_requote_sec = 10
mm_requote_threshold = 0.02
mm_fill_timeout_sec = 120
mm_bias_enabled = true
mm_bias_strength = 0.02
mm_paper_mode = true
mm_max_daily_loss = 20
mm_min_time_remaining_sec = 180
```

---

## Paso 4: API Routes

Agregar en `src/api/routes.py`:

### GET `/api/market-maker/status`
```python
@router.get("/api/market-maker/status")
# Retorna: get_status() del CryptoMarketMaker
# Incluye: enabled, paper_mode, active_markets, open_orders, inventory, pnl, stats
```

### GET `/api/market-maker/config`
```python
@router.get("/api/market-maker/config")
# Cargar config desde DB (get_config_bulk con keys mm_*)
```

### POST `/api/market-maker/config`
```python
@router.post("/api/market-maker/config")
# Guardar config en DB + recargar en CryptoMarketMaker
# Body: {mm_enabled, mm_bet_size, mm_spread_target, ...}
```

### GET `/api/market-maker/trades`
```python
@router.get("/api/market-maker/trades")
# Historial de trades MM: db.get_mm_trades(hours=48)
```

### POST `/api/market-maker/reset`
```python
@router.post("/api/market-maker/reset")
# Cancelar todas las órdenes abiertas + resetear stats
```

---

## Paso 5: Integración en `main.py`

### Cambios en `__init__`:
```python
# Reemplazar:
self.market_maker = None
# Por:
self.crypto_market_maker = None  # Nuevo nombre para diferenciar
```

### Cambios en `_start_crypto_arb()`:
```python
# Después de inicializar autotrader y paper_trader, agregar:
from src.strategies.market_maker import CryptoMarketMaker
self.crypto_market_maker = CryptoMarketMaker(self.db, self.binance_feed, self.crypto_detector)
await self.crypto_market_maker.initialize()
```

### Nuevo loop dedicado `_run_market_maker()`:
```python
async def _run_market_maker(self):
    """Loop dedicado para market maker crypto — tick cada 5s."""
    await asyncio.sleep(10)  # Esperar que feed y detector tengan datos
    while self._running and self.crypto_market_maker:
        try:
            await self.crypto_market_maker.tick()
        except Exception as e:
            print(f"[MarketMaker] Error en tick: {e}", flush=True)
        await asyncio.sleep(5)
```

### Lanzar como task en `_start_crypto_arb()`:
```python
asyncio.create_task(self._run_market_maker())
```

### Eliminar el tick del market_maker viejo del polling loop:
```python
# Líneas 607-608 en run_polling_loop:
# ANTES:
if self.market_maker:
    await self.market_maker.tick()
# DESPUÉS:
# (eliminado — el nuevo market maker tiene su propio loop)
```

---

## Paso 6: Dashboard UI

Agregar en `src/dashboard/index.html`:

### Nuevo sub-tab en sección Crypto:
- Tab: `csub-mm` → Panel: `cpanel-mm`
- Título: "🏪 Market Making"

### Contenido del panel:

**Sección Config:**
```
[Toggle] Habilitado
[Toggle] Paper Mode
[Input]  Bet Size por lado ($)
[Input]  Spread Target (¢)
[Input]  Max Mercados Simultáneos
[Input]  Max Inventario ($)
[Input]  Re-quote cada (seg)
[Input]  Timeout de fill (seg)
[Toggle] Sesgo Direccional
[Input]  Fuerza del sesgo (¢)
[Input]  Max Pérdida Diaria ($)
[Botón]  Guardar Config
[Botón]  Resetear Stats
```

**Sección Stats (auto-refresh 10s):**
```
| Stat              | Valor       |
|-------------------|-------------|
| Estado            | 🟢 Activo / Paper / Pausado |
| Mercados activos  | 3           |
| Órdenes abiertas  | 6 (3 pares) |
| PnL Hoy           | +$4.50      |
| PnL Total         | +$23.80     |
| Fills             | 47          |
| Re-quotes         | 12          |
| Rebates est.      | +$1.20      |
| Spread promedio    | 3.8¢        |
```

**Sección Inventario (tabla):**
```
| Mercado                    | Up Shares | Down Shares | Neto | Costo | PnL Est. |
|----------------------------|-----------|-------------|------|-------|----------|
| BTC 15m 1740000000         | 5.0       | 5.0         | 0    | $4.80 | +$0.20   |
| ETH 15m 1740000900         | 5.0       | 0           | +5   | $2.40 | pending  |
```

**Sección Historial de Trades (tabla):**
```
| Fecha    | Mercado    | Lado | Precio | Shares | USDC  | Status    | PnL    |
|----------|------------|------|--------|--------|-------|-----------|--------|
| 14:30:05 | BTC Up     | BUY  | $0.48  | 10.4   | $5.00 | filled    | +$0.20 |
| 14:30:05 | BTC Down   | BUY  | $0.48  | 10.4   | $5.00 | filled    | -$0.20 |
```

### JavaScript:
```javascript
// Funciones:
async function loadMMConfig()     // GET /api/market-maker/config → llenar inputs
async function saveMMConfig()     // POST /api/market-maker/config
async function loadMMStatus()     // GET /api/market-maker/status → actualizar stats + inventario
async function loadMMTrades()     // GET /api/market-maker/trades → tabla historial
async function resetMMStats()     // POST /api/market-maker/reset

// Auto-refresh: cada 10s si el tab está visible
```

---

## Paso 7: Sesgo Direccional (Integración con Detector)

En `CryptoMarketMaker.tick()`, cuando hay señales del `CryptoArbDetector`:

```
def _get_directional_bias(self, cid: str) -> float:
    """Retorna sesgo: >0 = favorecer Up, <0 = favorecer Down, 0 = neutral."""
    if not self._config.get("mm_bias_enabled"):
        return 0
    
    # Buscar señal reciente para este mercado
    signals = self._detector.get_recent_signals(50)
    for s in signals:
        if s["condition_id"] == cid:
            direction = s.get("direction", "")
            confidence = s.get("confidence", 0)
            if confidence >= 60:
                bias = self._config.get("mm_bias_strength", 0.02)
                return bias if direction == "up" else -bias
    return 0
```

Uso en quoting:
```
bias = self._get_directional_bias(cid)
up_bid = mid_price - spread/2 + bias    # Más agresivo si bias > 0
down_bid = mid_price - spread/2 - bias  # Más defensivo si bias > 0
```

---

## Ejemplo Numérico

### Sin señal (simétrico):
```
Mercado: BTC 15min Up/Down
Precio Up = 0.52, Precio Down = 0.48
Mid = 0.50, Spread target = 0.04

BUY Up  @ 0.48  → 10.4 shares ($5)
BUY Down @ 0.48 → 10.4 shares ($5)
Total invertido: $10

Si ambos llenan:
- Up resuelve "Up" → Up shares pagan $1 = $10.40, Down shares = $0
  PnL = $10.40 - $10.00 = +$0.40 + rebates (~$0.05) = +$0.45
  
- Down resuelve "Down" → Down shares pagan $1 = $10.40, Up shares = $0
  PnL = $10.40 - $10.00 = +$0.40 + rebates (~$0.05) = +$0.45
```

### Con señal "Up" (sesgado):
```
bias = +0.02
BUY Up  @ 0.50  → 10.0 shares ($5)  ← más agresivo (llena más fácil)
BUY Down @ 0.46 → 10.9 shares ($5)  ← más defensivo

Si Up gana: PnL = 10.0 * $1 - $10 = $0 + rebates
Si Down gana: PnL = 10.9 * $1 - $10 = +$0.90 + rebates (pero menos probable)
```

---

## Orden de Implementación

1. **`src/strategies/market_maker.py`** — Reescribir clase completa (~400 líneas)
2. **`src/storage/database.py`** — Tabla `mm_trades` + 4 funciones
3. **`src/config.py`** — Constantes MM_* actualizadas + restore_from_db
4. **`src/api/routes.py`** — 5 endpoints nuevos
5. **`src/main.py`** — Integrar nuevo market maker + loop dedicado
6. **`src/dashboard/index.html`** — Sub-tab UI completo
7. **Testing** — Paper mode primero, luego real con $5 por lado

---

## Riesgos y Mitigaciones

| Riesgo | Mitigación |
|--------|------------|
| Perder en ambos lados si spread es negativo | Verificar que up_bid + down_bid < $1.00 SIEMPRE |
| Inventario desbalanceado (fills parciales) | Cancelar lado sin fill después de timeout |
| MIN_CLOB_SHARES = 5 fuerza gasto mayor | Calcular cost real ANTES de quotear (ya corregido) |
| Mercado resuelve antes de fill bilateral | Solo quotear mercados con > 3 min restantes |
| API rate limiting | Re-quotear cada 10s (no 1s), usar rate limiter existente |
| Pérdida excesiva | max_daily_loss pausa automáticamente |

---

## Tiempo Estimado

| Paso | Tiempo |
|------|--------|
| market_maker.py completo | 45 min |
| DB + Config | 15 min |
| API Routes | 15 min |
| main.py integración | 10 min |
| Dashboard UI | 30 min |
| Testing + fixes | 15 min |
| **Total** | **~2.5 horas** |
