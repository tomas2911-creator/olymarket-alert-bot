# Wallet Scan Upgrade — Plan de Implementación Completo

> **Estado**: PENDIENTE APROBACIÓN — No se hará ningún cambio hasta que se apruebe.
> **Fecha**: 2026-02-23

---

## Índice

1. [Resumen Ejecutivo](#1-resumen-ejecutivo)
2. [Fase 1 — Nuevas métricas con datos que YA tenemos](#2-fase-1)
3. [Fase 2 — Leaderboard multi-período](#3-fase-2)
4. [Fase 3 — Endpoint /trades para análisis profundo](#4-fase-3)
5. [Fase 4 — Capital real via Etherscan V2](#5-fase-4)
6. [Fase 5 — Score mejorado](#6-fase-5)
7. [Fase 6 — Badges y filtros automáticos](#7-fase-6)
8. [Fase 7 — Dashboard rediseño](#8-fase-7)
9. [Schema DB — Migraciones](#9-schema-db)
10. [Orden de ejecución](#10-orden)

---

## 1. Resumen Ejecutivo

### Problemas actuales:
- **Capital Inicio** = volumen total operado (INCORRECTO, infla x100)
- **ROI** calculado sobre volumen → siempre bajísimo y mentiroso
- **Score** simple (4 factores, 1-5 estrellas) → no predice rentabilidad futura
- **Win Rate** sin ponderar por tamaño → trade de +$1 = trade de +$100K
- **Sin métricas clave**: Profit Factor, Max Drawdown, PnL reciente, hold time
- **Sin detección**: market makers, inactivas, one-hit wonders

### Solución: 7 fases incrementales, cada una desplegable independientemente.

---

## 2. Fase 1 — Nuevas métricas con datos que YA tenemos (sin APIs nuevas)

**Impacto: 🔴 ALTO | Esfuerzo: BAJO**

Calculamos métricas nuevas usando los datos de `/closed-positions` que ya descargamos (hasta 2500 posiciones cerradas con `realizedPnl`, `totalBought`, `timestamp`).

### 2.1 Profit Factor
```
sum_wins = sum(realizedPnl for cp where realizedPnl > 0)
sum_losses = abs(sum(realizedPnl for cp where realizedPnl < 0))
profit_factor = sum_wins / max(sum_losses, 1)
```
- **> 2.0** = excelente trader
- **1.0 - 2.0** = promedio
- **< 1.0** = pierde dinero

### 2.2 Avg Trade Size
```
avg_trade_size = sum(totalBought for all closed + open) / total_trades
```
Indica con cuánto opera típicamente. Crucial para saber si podés copiarla.

### 2.3 Max Drawdown (aproximado)
```python
# Recorrer closed_positions ordenadas por timestamp ASC
running_pnl = 0
peak = 0
max_dd = 0
for cp in closed_positions_sorted_by_timestamp_asc:
    running_pnl += cp.realizedPnl
    peak = max(peak, running_pnl)
    dd = peak - running_pnl
    max_dd = max(max_dd, dd)
```
Muestra la peor racha de pérdidas acumulada. Crítico para gestión de riesgo.

### 2.4 Win Rate ponderado por tamaño
```
weighted_wins = sum(totalBought for cp where realizedPnl > 0)
weighted_total = sum(totalBought for all cp)
weighted_wr = weighted_wins / max(weighted_total, 1) * 100
```
Un win de $10K pesa más que un win de $1.

### 2.5 Última actividad
```
last_trade_ts = closed_positions[0].timestamp  # ya están DESC
```
Filtrar wallets muertas (> 30 días sin actividad).

### Archivos a modificar:
| Archivo | Cambio |
|---|---|
| `src/api/routes.py` → `_scan_wallet_for_batch()` | Calcular profit_factor, avg_trade_size, max_drawdown, weighted_wr, last_trade_ts |
| `src/api/routes.py` → wallet_scan individual | Igual + retornar en response |
| `src/storage/database.py` → tabla `wallet_scan_cache` | Agregar columnas (ALTER TABLE) |
| `src/storage/database.py` → `save_scan_result()` | Guardar campos nuevos |
| `src/storage/database.py` → `get_scan_results()` | Incluir en filtros y sorts |

---

## 3. Fase 2 — Leaderboard multi-período (PnL 7d/30d + categorías)

**Impacto: 🔴 ALTO | Esfuerzo: BAJO**

El endpoint `/v1/leaderboard` acepta `period=DAY|WEEK|MONTH|ALL` y `category=WEATHER|SPORTS|POLITICS|CRYPTO|...`. Ya lo usamos con `ALL`, solo hay que hacer calls extra.

### 3.1 PnL por período
Agregar al batch scan:
```python
async def _lb_week():
    r = await client.get(f"{_POLY_DATA_API}/v1/leaderboard",
                         params={"user": addr, "period": "WEEK", "order": "PNL"})
    ...

async def _lb_month():
    r = await client.get(f"{_POLY_DATA_API}/v1/leaderboard",
                         params={"user": addr, "period": "MONTH", "order": "PNL"})
    ...
```

Campos nuevos: `pnl_7d`, `pnl_30d`, `vol_7d`, `vol_30d`

### 3.2 PnL por categoría (scan individual, NO batch)
Solo para el scan individual (click en wallet), no batch (demasiadas calls):
```python
categories = ["WEATHER", "SPORTS", "POLITICS", "CRYPTO"]
for cat in categories:
    r = await client.get(f"{_POLY_DATA_API}/v1/leaderboard",
                         params={"user": addr, "period": "ALL", "category": cat, "order": "PNL"})
```

### Archivos a modificar:
| Archivo | Cambio |
|---|---|
| `src/api/routes.py` → `_scan_wallet_for_batch()` | Agregar _lb_week() y _lb_month() al gather |
| `src/api/routes.py` → wallet_scan individual | Agregar category breakdown |
| `src/storage/database.py` | Agregar columnas pnl_7d, pnl_30d, vol_7d, vol_30d |

### Rate limiting
Esto agrega 2 calls extra por wallet al batch. Con semáforo de 3 y ~200 wallets = ~400 calls extra. Polymarket no tiene rate limit documentado pero ser conservador: **bajar semáforo a 2** si es necesario.

---

## 4. Fase 3 — Endpoint /trades para análisis profundo

**Impacto: 🟡 MEDIO | Esfuerzo: MEDIO**

El endpoint `/trades?user={addr}` devuelve trades individuales con:
```json
{ "side": "BUY|SELL", "size": 123, "price": 0.65, "timestamp": 1708000000,
  "conditionId": "0x...", "title": "...", "transactionHash": "0x..." }
```

### 4.1 Frecuencia de trading
```
trades_per_week = total_trades_from_api / max(weeks_active, 1)
```

### 4.2 Avg hold time (solo scan individual, es costoso)
```python
# Agrupar trades por conditionId
# Para cada mercado: first_buy_ts → last_sell_ts (o close_ts)
# avg_hold_time = promedio de diferencias
```

### 4.3 Buy/Sell ratio
```
buy_count = len([t for t in trades if t.side == "BUY"])
sell_count = len([t for t in trades if t.side == "SELL"])
buy_sell_ratio = buy_count / max(sell_count, 1)
```
- Ratio alto = buy and hold
- Ratio ~1 = trader activo o market maker

### Para batch scan:
Solo usar `/trades?user={addr}&limit=1` para obtener timestamp del último trade (más rápido que paginar closed-positions). O usar `limit=100` para una muestra rápida.

### Para scan individual:
Usar `limit=10000` para análisis completo de hold time, frecuencia, etc.

### Archivos a modificar:
| Archivo | Cambio |
|---|---|
| `src/api/routes.py` → `_scan_wallet_for_batch()` | Agregar _trades() con limit=100 |
| `src/api/routes.py` → wallet_scan individual | Agregar _trades() con limit=10000 |
| `src/storage/database.py` | Agregar columnas trades_per_week, buy_sell_ratio, last_trade_ts |

---

## 5. Fase 4 — Capital real via Etherscan V2 (PolygonScan)

**Impacto: 🔴 CRÍTICO | Esfuerzo: MEDIO**

### El problema fundamental
`estimated_initial` actualmente usa `official_volume` del leaderboard. Esto es el volumen total operado, NO el capital que depositó. Una wallet con $1K que hizo 100 trades de $1K tiene vol=$100K pero capital=$1K.

### Solución: USDC token transfers on-chain

**Endpoint**: Etherscan V2 API (unificado para todas las chains)
```
GET https://api.etherscan.io/v2/api
  ?chainid=137
  &module=account
  &action=tokentx
  &address={PROXY_WALLET}
  &contractaddress={USDC_CONTRACT}
  &sort=asc
  &apikey={ETHERSCAN_API_KEY}
```

**USDC en Polygon**:
- PoS bridged: `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` (USDC.e, 6 decimals)
- Native: `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359` (USDC, 6 decimals)

### Lógica de capital real:
```python
deposits = 0  # USDC transfers TO the wallet
withdrawals = 0  # USDC transfers FROM the wallet

for tx in usdc_transfers:
    amount = int(tx["value"]) / 1e6  # 6 decimals
    if tx["to"].lower() == wallet_addr.lower():
        deposits += amount
    elif tx["from"].lower() == wallet_addr.lower():
        withdrawals += amount

real_capital = deposits  # Total capital ever deposited
net_capital = deposits - withdrawals  # Capital still "in play"
```

### Complicaciones:
1. La wallet de Polymarket es un **proxy wallet**, no la wallet principal. Los transfers de USDC al proxy son deposits al exchange.
2. También hay transfers internos (Polymarket contracts ↔ proxy) que hay que filtrar.
3. El contrato de Polymarket exchange: `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` — transfers desde/hacia este contrato son operaciones, NO deposits.

### Filtro:
```python
POLYMARKET_CONTRACTS = {
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",  # Exchange
    "0x..." # Otros contratos conocidos de Polymarket
}
# Solo contar transfers que NO son de/hacia contratos de Polymarket
is_real_deposit = tx["to"] == wallet AND tx["from"] NOT IN POLYMARKET_CONTRACTS
is_real_withdrawal = tx["from"] == wallet AND tx["to"] NOT IN POLYMARKET_CONTRACTS
```

### Fallback (si no hay Etherscan key):
```python
# Mejor aproximación sin on-chain data:
estimated_capital = max(
    portfolio_value - total_pnl,                    # Si nunca retiró
    max(cp.totalBought for cp in closed_positions), # Tamaño máximo de una posición
    0
)
```

### Requisitos:
- **API key de Etherscan** (gratis: https://etherscan.io/register) — 5 calls/seg
- Configurar en `.env` o `config.yaml`: `ETHERSCAN_API_KEY=xxx`

### ROI corregido:
```python
roi_real = total_pnl / max(real_capital, 1) * 100
```

### Archivos a modificar:
| Archivo | Cambio |
|---|---|
| `src/config.py` | Cargar ETHERSCAN_API_KEY |
| `config.yaml` | Agregar sección etherscan |
| `src/api/routes.py` | Nueva función _fetch_usdc_transfers() |
| `src/api/routes.py` → ambos scans | Calcular real_capital, corregir roi_pct |
| `src/storage/database.py` | Agregar columnas real_capital, deposits, withdrawals |

### Rate limiting para batch:
Etherscan = 5 calls/seg. Con 200 wallets = ~200 calls × 2 (ambos USDC contracts) = 400 calls / 5 = 80 seg extra.
**Agregar semáforo dedicado** para Etherscan: `Semaphore(4)` para no exceder 5/seg.

---

## 6. Fase 5 — Score mejorado (reemplazar estrellas)

**Impacto: 🔴 ALTO | Esfuerzo: MEDIO**

### Sistema actual:
```python
def _calc_wallet_score(result) -> int:  # 1-5 estrellas
    # 4 factores simples con umbrales fijos
    # win_rate >= 70 → +2, PnL >= 10000 → +2, trades >= 100 → +1, roi >= 30 → +1
```

### Nuevo sistema: Score compuesto 0-100

```python
def _calc_wallet_score_v2(result: dict) -> dict:
    """Score compuesto 0-100 con componentes desglosados."""

    # ── Componentes (cada uno 0-100, ponderados) ──

    # 1. Profit Factor Score (peso: 25%)
    pf = result.get("profit_factor", 0)
    pf_score = min(100, max(0, (pf - 0.5) * 40))  # PF=3.0 → 100

    # 2. Win Rate ponderado (peso: 15%)
    wwr = result.get("weighted_win_rate", 50)
    wr_score = min(100, max(0, (wwr - 30) * 1.43))  # 30%→0, 100%→100

    # 3. Consistencia: PnL 30d vs PnL ALL (peso: 15%)
    pnl_all = result.get("total_pnl", 0)
    pnl_30d = result.get("pnl_30d", 0)
    if pnl_all > 0:
        recent_ratio = pnl_30d / max(pnl_all, 1)
        cons_score = min(100, max(0, recent_ratio * 200))  # 50% en 30d = 100
    else:
        cons_score = max(0, min(100, pnl_30d * 0.1))  # Al menos gana reciente

    # 4. Volumen/Experiencia (peso: 10%)
    trades = result.get("total_trades", 0)
    exp_score = min(100, trades * 1.0)  # 100 trades = 100

    # 5. Riesgo (Max Drawdown inverso) (peso: 15%)
    dd = result.get("max_drawdown", 0)
    pnl = max(abs(pnl_all), 1)
    dd_ratio = dd / pnl
    risk_score = max(0, 100 - dd_ratio * 100)  # DD=0 → 100, DD=PnL → 0

    # 6. ROI real (peso: 15%)
    roi = result.get("roi_pct", 0)
    roi_score = min(100, max(0, roi * 2))  # 50% ROI → 100

    # 7. Actividad reciente (peso: 5%)
    days_since = result.get("days_since_last_trade", 999)
    activity_score = max(0, 100 - days_since * 3.3)  # 0d→100, 30d→0

    # ── Score final ponderado ──
    total = (
        pf_score * 0.25 +
        wr_score * 0.15 +
        cons_score * 0.15 +
        exp_score * 0.10 +
        risk_score * 0.15 +
        roi_score * 0.15 +
        activity_score * 0.05
    )

    # Penalizaciones:
    if result.get("is_market_maker"):
        total *= 0.1  # MM = score casi 0
    if trades < 5:
        total *= 0.3  # One-hit wonder penalty
    if days_since > 60:
        total *= 0.5  # Inactiva penalty

    return {
        "score": round(total),
        "components": {
            "profit_factor": round(pf_score),
            "win_rate": round(wr_score),
            "consistency": round(cons_score),
            "experience": round(exp_score),
            "risk": round(risk_score),
            "roi": round(roi_score),
            "activity": round(activity_score),
        },
        "grade": _score_to_grade(total),  # S, A, B, C, D, F
    }

def _score_to_grade(score: float) -> str:
    if score >= 85: return "S"   # Elite
    if score >= 70: return "A"   # Muy buena
    if score >= 55: return "B"   # Buena
    if score >= 40: return "C"   # Regular
    if score >= 25: return "D"   # Mala
    return "F"                    # Evitar
```

### Visualización en tabla batch:
**Reemplazar estrellas ⭐** por **badge de letra con color**:
- **S** = 🟣 violeta (elite)
- **A** = 🟢 verde
- **B** = 🔵 azul
- **C** = 🟡 amarillo
- **D** = 🟠 naranja
- **F** = 🔴 rojo

Además mostrar el score numérico: `A 78`

### Archivos a modificar:
| Archivo | Cambio |
|---|---|
| `src/api/routes.py` → `_calc_wallet_score` | Reemplazar por _calc_wallet_score_v2 |
| `src/storage/database.py` | score cambia de INTEGER a DOUBLE, agregar grade TEXT |
| Dashboard tabla batch | Badge con letra y color en vez de estrellas |
| Dashboard scan individual | Desglose radar/barras de componentes |

---

## 7. Fase 6 — Badges y filtros automáticos

**Impacto: 🟡 MEDIO | Esfuerzo: BAJO**

### Badges automáticos (calculados, no almacenados):
```python
def _calc_badges(result: dict) -> list[str]:
    badges = []

    # 🏆 Elite: WR≥65%, trades≥50, PF≥2.0, activa últimos 7d
    if (result.get("weighted_win_rate", 0) >= 65
        and result.get("total_trades", 0) >= 50
        and result.get("profit_factor", 0) >= 2.0
        and result.get("days_since_last_trade", 999) <= 7):
        badges.append("elite")

    # 📈 Hot Streak: PnL 7d > 0 y PnL 30d > 0 y ambos creciendo
    if result.get("pnl_7d", 0) > 0 and result.get("pnl_30d", 0) > 0:
        badges.append("hot")

    # ⚠️ Market Maker: vol>$100K, PnL/vol < 0.5%, trades>50
    vol = result.get("official_volume", 0) or result.get("estimated_initial", 0)
    if (vol > 100000
        and result.get("total_trades", 0) > 50
        and abs(result.get("total_pnl", 0)) / max(vol, 1) < 0.005):
        badges.append("market_maker")

    # 💀 Inactiva: último trade > 30 días
    if result.get("days_since_last_trade", 999) > 30:
        badges.append("inactive")

    # 🎰 One-Hit: < 5 trades resueltos
    if result.get("wins", 0) + result.get("losses", 0) < 5:
        badges.append("one_hit")

    # 🐋 Whale: portfolio > $100K
    if result.get("portfolio_value", 0) > 100000:
        badges.append("whale")

    # 📉 En racha perdedora: PnL 7d < 0 y PnL 30d < 0
    if result.get("pnl_7d", 0) < 0 and result.get("pnl_30d", 0) < 0:
        badges.append("cold")

    return badges
```

### Visualización:
```
🏆 Elite  📈 Hot  ⚠️ MM  💀 Inactiva  🎰 OneHit  🐋 Whale  📉 Cold
```

Cada badge es un tag coloreado que aparece debajo del nombre en la tabla batch.

### Filtro rápido en dashboard:
Agregar dropdown de badges al panel de filtros existente:
```html
<select id="bscan-filter-badge">
    <option value="">Todos</option>
    <option value="elite">🏆 Elite</option>
    <option value="hot">📈 Hot</option>
    <option value="whale">🐋 Whale</option>
    <option value="!market_maker">❌ No MM</option>
    <option value="!inactive">❌ No Inactivas</option>
</select>
```

### Archivos a modificar:
| Archivo | Cambio |
|---|---|
| `src/api/routes.py` | Nueva función _calc_badges(), llamar al guardar |
| `src/storage/database.py` | Agregar columna badges TEXT (JSON array string) |
| Dashboard filtros | Agregar dropdown de badges |
| Dashboard tabla batch | Mostrar badges debajo del nombre |

---

## 8. Fase 7 — Dashboard rediseño

**Impacto: 🟡 MEDIO | Esfuerzo: MEDIO-ALTO**

### 8.1 Tabla Batch — ANTES (actual):
```
| # | Wallet | Origen | Trades | Win Rate | PnL Total | ROI | Portfolio | Score ⭐ | 🔍 |
```

### 8.1 Tabla Batch — DESPUÉS (nueva):
```
| # | Wallet + Badges | PnL Total | PnL 30d | PF | WR% | ROI | Portfolio | Score | 🔍 |
|   |                  |           |         |    |     | (real) |        | A 78  |    |
```

**Columnas nuevas**: PnL 30d, PF (Profit Factor), Score con letra
**Columnas removidas**: Origen (mover a tooltip), Trades (mover a tooltip)
**Modificadas**: ROI (usar real si disponible), Score (letra+número en vez de estrellas)

### Diseño de fila:
```
 1  | MirrorOfTheWorld           | +$862K  | +$42K | 2.8 | 68% | +59%  | $1.4M | A 78 | 🔍
    | 0x1234...abcd 🏆📈         |   ↑grn  | ↑grn  | grn | grn | grn   |       | ■grn |
```

- **Wallet**: nombre arriba, address + badges abajo (font 10px)
- **PnL 30d**: con color verde/rojo + flecha ↑↓
- **PF**: verde si > 2.0, amarillo 1-2, rojo < 1
- **Score**: badge `A` con fondo color + número

### 8.2 Scan individual — ANTES:
```
KPIs: Portfolio | Capital Inicial | PnL Total | ROI | Win Rate | Total Trades
Extras: W/L | PnL Realizado | PnL No Realizado | Primer Trade | Tiempo Activo
```

### 8.2 Scan individual — DESPUÉS:
```
KPIs Fila 1 (6 cards):
  Portfolio | Capital Real | PnL Total | ROI Real | Win Rate | Score A 78

KPIs Fila 2 (6 cards):
  PnL 7d | PnL 30d | Profit Factor | Max Drawdown | Avg Size | Trades/Semana

KPIs Fila 3 (5 cards):
  W / L | PnL Realizado | PnL No Realizado | Primer Trade | Última Actividad

Badges:
  🏆 Elite  📈 Hot Streak  🐋 Whale

Score Desglose (barra horizontal o mini radar):
  [█████████░] PF 90  [███████░░░] WR 70  [████░░░░░░] Risk 40 ...

Categorías (si hay data):
  WEATHER: +$12K ✅  |  SPORTS: -$3K ❌  |  POLITICS: +$800  |  CRYPTO: N/A

Posiciones Abiertas (sin cambios)
Actividad Reciente (sin cambios)
```

### 8.3 Stats resumidos arriba del batch:
```
ANTES: (nada relevante)
DESPUÉS:
  Total Escaneadas: 156 | Top S/A: 12 | PnL Promedio: +$5.2K | Activas 7d: 89 | Market Makers: 23
```

### 8.4 Filtros batch — agregar:
```
Filtros nuevos:
  - PnL 30d (min/max)
  - Profit Factor (min/max)
  - Badge (dropdown multiselect)
  - Score grade (S/A/B/C/D/F checkboxes)
  - Última actividad (max días)

Sort nuevos:
  - pnl_30d, profit_factor, score (ya existe pero ahora 0-100)
```

### Archivos a modificar:
| Archivo | Cambio |
|---|---|
| `src/dashboard/index.html` | Tabla batch: columnas nuevas |
| `src/dashboard/index.html` | Scan individual: KPIs nuevos + score desglose |
| `src/dashboard/index.html` | Filtros: agregar nuevos campos |
| `src/dashboard/index.html` | JS: _renderBatchTable(), renderWscanResult() |

---

## 9. Schema DB — Migraciones

### Columnas nuevas en `wallet_scan_cache`:

```sql
-- Fase 1: Métricas calculadas
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS profit_factor DOUBLE PRECISION DEFAULT 0;
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS avg_trade_size DOUBLE PRECISION DEFAULT 0;
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS max_drawdown DOUBLE PRECISION DEFAULT 0;
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS weighted_win_rate DOUBLE PRECISION DEFAULT 0;
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS last_trade_ts TIMESTAMPTZ;

-- Fase 2: Leaderboard multi-período
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS pnl_7d DOUBLE PRECISION DEFAULT 0;
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS pnl_30d DOUBLE PRECISION DEFAULT 0;
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS vol_7d DOUBLE PRECISION DEFAULT 0;
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS vol_30d DOUBLE PRECISION DEFAULT 0;

-- Fase 3: Trades analysis
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS trades_per_week DOUBLE PRECISION DEFAULT 0;
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS buy_sell_ratio DOUBLE PRECISION DEFAULT 0;

-- Fase 4: Capital real
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS real_capital DOUBLE PRECISION DEFAULT 0;
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS usdc_deposits DOUBLE PRECISION DEFAULT 0;
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS usdc_withdrawals DOUBLE PRECISION DEFAULT 0;
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS roi_real DOUBLE PRECISION DEFAULT 0;

-- Fase 5: Score mejorado
-- score ya existe como INTEGER → cambiar significado (ahora 0-100)
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS grade TEXT DEFAULT 'F';
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS score_components JSONB DEFAULT '{}';

-- Fase 6: Badges
ALTER TABLE wallet_scan_cache ADD COLUMN IF NOT EXISTS badges TEXT DEFAULT '[]';

-- Índices nuevos
CREATE INDEX IF NOT EXISTS idx_wscan_pnl30d ON wallet_scan_cache(pnl_30d);
CREATE INDEX IF NOT EXISTS idx_wscan_pf ON wallet_scan_cache(profit_factor);
CREATE INDEX IF NOT EXISTS idx_wscan_grade ON wallet_scan_cache(grade);
CREATE INDEX IF NOT EXISTS idx_wscan_last_trade ON wallet_scan_cache(last_trade_ts);
```

### Compatibilidad:
- Todas las columnas tienen DEFAULT → no rompe datos existentes
- `score` cambia de significado (1-5 → 0-100) pero es numérico en ambos → compatible
- La migración se hace en `_ensure_tables()` con `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`

---

## 10. Orden de ejecución

```
Fase 1 ─── Métricas con datos existentes ──── [~45 min]
  │         (profit_factor, avg_size, drawdown,
  │          weighted_wr, last_trade)
  ▼
Fase 2 ─── Leaderboard WEEK/MONTH ─────────── [~30 min]
  │         (pnl_7d, pnl_30d, vol_7d, vol_30d)
  ▼
Fase 5 ─── Score mejorado ─────────────────── [~30 min]
  │         (usa datos de F1+F2, reemplaza estrellas)
  ▼
Fase 6 ─── Badges automáticos ─────────────── [~20 min]
  │         (usa datos de F1+F2+F5)
  ▼
Fase 7a ── Dashboard: tabla batch ──────────── [~40 min]
  │         (columnas nuevas, score badge, badges)
  ▼
Fase 7b ── Dashboard: scan individual ──────── [~40 min]
  │         (KPIs nuevos, desglose score, categorías)
  ▼
Fase 3 ─── Endpoint /trades ───────────────── [~30 min]
  │         (trades_per_week, buy_sell_ratio, hold_time)
  ▼
Fase 4 ─── Etherscan capital real ──────────── [~45 min]
  │         (REQUIERE API KEY - se puede saltar)
  │         (real_capital, deposits, withdrawals, roi_real)
  ▼
Fase 7c ── Dashboard: filtros + stats ──────── [~30 min]
            (filtros nuevos, stats header)

TOTAL ESTIMADO: ~5 horas
```

### ¿Por qué este orden?
1. **F1 primero**: usa datos que ya descargamos, cero APIs nuevas, máximo impacto
2. **F2 después**: solo 2 calls HTTP extra, datos muy valiosos
3. **F5 antes de F7**: el dashboard necesita saber cómo se renderiza el score
4. **F6 antes de F7**: los badges se muestran en el dashboard
5. **F3 después de F7**: el dashboard ya funciona, esto es enriquecimiento
6. **F4 al final**: requiere API key externa, es opcional si no la tienen

### Testing entre fases:
Después de cada fase, un re-scan de 5 wallets conocidas para verificar que los datos nuevos son correctos. No se necesita test unitario formal — verificación visual en dashboard.

---

## Config nueva necesaria

### En `config.yaml`:
```yaml
wallet_scan:
  batch_concurrency: 3          # Semáforo para Polymarket API
  etherscan_concurrency: 4      # Semáforo para Etherscan (max 5/seg)
  max_closed_positions: 2500    # Límite de posiciones cerradas a paginar
  max_trades_batch: 100         # Trades a bajar en batch (sample)
  max_trades_individual: 10000  # Trades a bajar en scan individual
```

### En `.env`:
```
ETHERSCAN_API_KEY=your_key_here  # Gratis en etherscan.io/register
```

---

## Resumen de impacto en archivos

| Archivo | Fases | Tipo de cambio |
|---|---|---|
| `src/api/routes.py` | F1,F2,F3,F4,F5,F6 | Backend: nuevas métricas, APIs, scoring |
| `src/storage/database.py` | F1,F2,F3,F4,F5,F6 | Schema: nuevas columnas, migrations |
| `src/dashboard/index.html` | F7a,F7b,F7c | Frontend: tabla, KPIs, filtros, JS |
| `src/config.py` | F4 | Cargar ETHERSCAN_API_KEY |
| `config.yaml` | F4 | Sección wallet_scan |

**Total archivos modificados: 5**
**Archivos nuevos: 0** (todo se integra en código existente)
