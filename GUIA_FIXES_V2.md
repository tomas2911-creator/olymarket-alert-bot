# 🛠️ GUÍA DE IMPLEMENTACIÓN — Fixes Priorizados del Bot Crypto

> **ESTADO**: Pendiente de aprobación del usuario antes de implementar.
> **Fecha**: 21 Feb 2026
> **Objetivo**: Eliminar las causas raíz de las pérdidas (~$100) convirtiendo el bot sniper
> de taker puro a maker-first con cálculo de fees real.

---

## RESUMEN EJECUTIVO

El bot pierde dinero por 3 razones principales:
1. **No descuenta taker fees** (~1.5% por trade) del cálculo de edge
2. **fair_odds inventados** sin base estadística → edge percibido es ilusión
3. **Todas las órdenes son FOK (taker)** → paga fee máximo + slippage

### Estrategia de fix: **Maker-First Sniper**
- Cuando detecta señal, postea **orden GTC por debajo del ask** (maker, 0% fee)
- Calcula edge **neto de fees** antes de decidir
- Reduce fair_odds a valores **conservadores y realistas**
- Añade **consulta dinámica de fee_rate** al CLOB

---

## PASO 1: Función de cálculo de taker fee
**Archivo**: `src/crypto_arb/autotrader.py`
**Ubicación**: Después de las constantes (línea ~29, después de `POLYGON_RPC`)
**Acción**: Agregar función helper

```python
def _calc_taker_fee(price: float, fee_rate: float = 0.0625) -> float:
    """Calcular taker fee de Polymarket para mercados crypto.
    fee = p × (1-p) × fee_rate
    Retorna fee en USD por share.
    """
    if price <= 0 or price >= 1:
        return 0
    return round(price * (1 - price) * fee_rate, 6)
```

**Por qué**: Esta función calcula el fee exacto por share según la fórmula oficial
de Polymarket. Se usará en evaluate_signal y execute_trade para descontar fees
del edge antes de tomar decisiones.

---

## PASO 2: Consultar fee_rate dinámico del CLOB
**Archivo**: `src/crypto_arb/autotrader.py`
**Ubicación**: Dentro de la clase AutoTrader, después de `_get_best_ask` (~línea 710)
**Acción**: Agregar nuevo método

```python
async def _get_fee_rate(self, token_id: str) -> float:
    """Consultar fee_rate_bps del CLOB para un token.
    GET https://clob.polymarket.com/fee-rate?token_id={token_id}
    Retorna el fee_rate como multiplicador (ej: 0.0625).
    Si falla, retorna el default 0.0625.
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{CLOB_HOST}/fee-rate",
                params={"token_id": token_id}
            )
            if resp.status_code == 200:
                data = resp.json()
                # fee_rate_bps viene en basis points, convertir
                bps = float(data.get("fee_rate_bps", 625))
                return bps / 10000  # 625 bps → 0.0625
    except Exception as e:
        print(f"[AutoTrader] _get_fee_rate error: {e}", flush=True)
    return 0.0625  # Default para crypto markets
```

**Por qué**: Polymarket puede cambiar el fee rate. La documentación dice
"fetch fee_rate_bps dynamically per token and do not hardcode it".
Usamos 0.0625 como fallback seguro.

---

## PASO 3: Descontar fee del edge en evaluate_signal
**Archivo**: `src/crypto_arb/autotrader.py`
**Ubicación**: Dentro de `evaluate_signal()`, DESPUÉS del filtro de edge (líneas 306-310)
**Acción**: Reemplazar el bloque de filtro de edge con versión fee-aware

**ANTES** (líneas 306-310):
```python
        # Filtro: edge mínimo
        edge = signal.get("edge_pct", 0)
        if edge < cfg["min_edge"]:
            print(f"{tag} SKIP: edge {edge}% < min {cfg['min_edge']}%", flush=True)
            return None
```

**DESPUÉS**:
```python
        # Filtro: edge mínimo (descontando taker fee estimado)
        edge = signal.get("edge_pct", 0)
        poly_odds_raw = signal.get("poly_odds", 0.5)
        taker_fee_per_share = _calc_taker_fee(poly_odds_raw)
        # Fee como % del upside potencial: fee / (1 - price) × 100
        upside = max(1.0 - poly_odds_raw, 0.01)
        fee_pct = (taker_fee_per_share / upside) * 100
        net_edge = round(edge - fee_pct, 1)
        if net_edge < cfg["min_edge"]:
            print(f"{tag} SKIP: net_edge {net_edge}% (bruto={edge}% - fee={fee_pct:.1f}%) < min {cfg['min_edge']}%", flush=True)
            return None
        # Reemplazar edge con net_edge para el resto del flujo
        edge = net_edge
```

**Por qué**: El edge que llega del detector es BRUTO (fair_odds - poly_odds).
No descuenta el fee que Polymarket cobra al comprar. Esto hacía que el bot
creyera tener 15% de edge cuando en realidad tenía ~12% (o menos con slippage).
Ahora el filtro usa el edge NETO.

---

## PASO 4: Reducir fair_odds a valores conservadores
**Archivo**: `src/crypto_arb/detector.py`
**Ubicación**: Dentro de `_check_sniper_strategy()`, líneas 828-838
**Acción**: Reemplazar la tabla de fair_odds

**ANTES** (líneas 828-838):
```python
            # Calcular fair_odds basado en el movimiento (backtested)
            if abs_move >= 0.10:
                fair_odds = 0.93
            elif abs_move >= 0.08:
                fair_odds = 0.90
            elif abs_move >= 0.05:
                fair_odds = 0.85
            elif abs_move >= 0.03:
                fair_odds = 0.78
            else:
                fair_odds = 0.70
```

**DESPUÉS**:
```python
            # Calcular fair_odds CONSERVADORES basados en el movimiento
            # Reducidos ~15% vs versión anterior para reflejar que el mercado
            # ya se ajustó parcialmente cuando el bot llega (55-150s delay)
            # y que necesitamos margen para fees + slippage
            if abs_move >= 0.15:
                fair_odds = 0.82
            elif abs_move >= 0.10:
                fair_odds = 0.78
            elif abs_move >= 0.08:
                fair_odds = 0.75
            elif abs_move >= 0.05:
                fair_odds = 0.70
            elif abs_move >= 0.03:
                fair_odds = 0.65
            else:
                fair_odds = 0.58
```

**Por qué**: Los fair_odds anteriores (0.78-0.93) eran demasiado optimistas.
El mercado de Polymarket ya incorpora la información del movimiento de Binance
en 1-10 segundos (bots HFT). Cuando nuestro bot llega a los 55-150s, las odds
ya reflejan parcialmente el movimiento. Los nuevos valores son ~15% más bajos,
lo que genera señales solo cuando hay edge REAL (odds baratas vs nuestra estimación).

---

## PASO 5: Cambiar sniper de FOK a GTC maker-first
**Archivo**: `src/crypto_arb/autotrader.py`
**Ubicación**: Dentro de `evaluate_signal()`, línea 392
**Acción**: Cambiar el order_type del sniper

**ANTES** (línea 392):
```python
        # Sniper siempre usa FOK (market) para ejecución inmediata
        order_type = "market" if strategy == "sniper" else cfg["order_type"]
```

**DESPUÉS**:
```python
        # Sniper usa GTC (limit) por defecto para actuar como maker (0% fee)
        # Solo usa FOK si el usuario desactiva sniper_use_maker
        if strategy == "sniper":
            use_maker = cfg.get("sniper_use_maker", True)
            order_type = "limit" if use_maker else "market"
        else:
            order_type = cfg["order_type"]
```

**Por qué**: FOK = taker = paga 1.56% fee. GTC = maker = 0% fee + posibles rebates.
El cambio más impactante en rentabilidad es dejar de pagar fees en cada trade.
Una orden GTC que se postea por debajo del ask se queda en el orderbook como maker.

---

## PASO 6: Precio maker inteligente para sniper GTC
**Archivo**: `src/crypto_arb/autotrader.py`
**Ubicación**: Dentro de `execute_trade()`, en el bloque de cálculo de order_price
  (líneas 472-484)
**Acción**: Modificar la lógica de pricing para GTC sniper

**ANTES** (líneas 472-484):
```python
            # FOK: agregar slippage para encontrar liquidez en el order book
            # El price en FOK BUY es el MÁXIMO que estamos dispuestos a pagar
            is_fok = order_mode == "market"
            if is_fok:
                # Sniper usa su propio slippage configurable
                if strategy == "sniper":
                    slippage_pct = self._config.get("sniper_slippage_pct", 5.0)
                else:
                    slippage_pct = self._config.get("slippage_max_pct", 3.0)
                slippage = round(slippage_pct / 100, 2)  # ej: 5% → 0.05
                order_price = min(round(price + slippage, 2), 0.99)
            else:
                order_price = price
```

**DESPUÉS**:
```python
            # Pricing según modo de orden
            is_fok = order_mode == "market"
            if is_fok:
                # FOK: agregar slippage (taker)
                if strategy == "sniper":
                    slippage_pct = self._config.get("sniper_slippage_pct", 5.0)
                else:
                    slippage_pct = self._config.get("slippage_max_pct", 3.0)
                slippage = round(slippage_pct / 100, 2)
                order_price = min(round(price + slippage, 2), 0.99)
            elif strategy == "sniper" and self._config.get("sniper_use_maker", True):
                # GTC Maker: postear POR DEBAJO del ask para ser maker (0% fee)
                # Usar spread_offset configurable (default 1 centavo)
                maker_offset = self._config.get("sniper_maker_offset", 0.01)
                order_price = round(max(price - maker_offset, 0.01), 2)
                print(f"[AutoTrader] 🏷️ Sniper MAKER price: ${order_price} "
                      f"(ask=${price} - offset=${maker_offset})", flush=True)
            else:
                order_price = price
```

**Por qué**: En vez de pagar slippage SOBRE el ask (taker), ahora posteamos
POR DEBAJO del ask (maker). Ejemplo: ask=$0.55, offset=$0.01 → orden a $0.54.
Si alguien vende contra nosotros, compramos a $0.54 sin fee.
El offset de $0.01 es configurable desde el dashboard.

---

## PASO 7: Timeout optimizado para GTC sniper
**Archivo**: `src/crypto_arb/autotrader.py`
**Ubicación**: Bloque de polling GTC (líneas 607-645)
**Acción**: El bloque existente ya maneja GTC con polling + cancelación.
Solo necesitamos ajustar el timeout default.

**ANTES** (línea 610):
```python
                gtc_timeout = self._config.get("sniper_gtc_timeout_sec", 40)
```

**DESPUÉS**:
```python
                gtc_timeout = self._config.get("sniper_gtc_timeout_sec", 30)
```

**Por qué**: En mercados de 5min, 40 segundos esperando fill es demasiado.
30 segundos es mejor: si no se llena en 30s, el mercado ya se movió
y el edge probablemente desapareció. Cancelamos y esperamos la siguiente oportunidad.

---

## PASO 8: Agregar config "sniper_use_maker" y "sniper_maker_offset"
**Archivo**: `src/crypto_arb/autotrader.py`
**Ubicación**: En `initialize()`, bloque de config sniper (líneas 114-126)
**Acción**: Agregar 2 nuevas config keys

**Agregar después de línea 126** (`"sniper_gtc_timeout_sec"`):
```python
                "sniper_use_maker": raw.get("at_sniper_use_maker", "true") == "true",
                "sniper_maker_offset": float(raw.get("at_sniper_maker_offset", 0.01)),
```

**Archivo**: `src/crypto_arb/autotrader.py`
**Ubicación**: En `initialize()`, bloque de lectura de DB (líneas 72-77)
**Acción**: Agregar las 2 keys al bulk read

**Agregar después de** `"at_sniper_gtc_timeout_sec",`:
```python
                "at_sniper_use_maker", "at_sniper_maker_offset",
```

**Por qué**: Para que el usuario pueda controlar desde el dashboard si quiere
modo maker (0% fee) o FOK (taker, ejecución inmediata), y el offset del precio maker.

---

## PASO 9: Recalcular edge con live_price Y fees en execute_trade
**Archivo**: `src/crypto_arb/autotrader.py`
**Ubicación**: Bloque de live_price en execute_trade (líneas 443-465)
**Acción**: Reescribir para incluir fee en el cálculo de edge real

**ANTES** (líneas 443-465):
```python
            # Opción: usar precio live del CLOB en vez del poly_odds stale de la señal
            strategy = trade_info.get("strategy", "score")
            if strategy == "sniper" and self._config.get("sniper_use_live_price", False) and live_price > 0:
                print(f"[AutoTrader] 📡 Live price: {live_price} (señal era {price})", flush=True)

                # Mejora 3: Max price cap — no comprar si live_price > 0.80
                if live_price > 0.80:
                    error_msg = f"Live price {live_price} > 0.80 (poco profit potencial)"
                    print(f"[AutoTrader] ❌ {error_msg}", flush=True)
                    return {"success": False, "error": error_msg}

                # Mejora 1: Recalcular edge con live_price real
                fair_odds = trade_info.get("fair_odds", 0)
                if fair_odds > 0:
                    real_edge = round((fair_odds - live_price) / live_price * 100, 1)
                    min_edge = self._config.get("min_edge", 5)
                    print(f"[AutoTrader] 📐 Edge recalculado: {real_edge}% (fair={fair_odds} live={live_price} min={min_edge}%)", flush=True)
                    if real_edge < min_edge:
                        error_msg = f"Edge real {real_edge}% < min {min_edge}% (live_price={live_price})"
                        print(f"[AutoTrader] ❌ {error_msg}", flush=True)
                        return {"success": False, "error": error_msg}

                price = live_price
```

**DESPUÉS**:
```python
            # Opción: usar precio live del CLOB en vez del poly_odds stale de la señal
            strategy = trade_info.get("strategy", "score")
            if strategy == "sniper" and self._config.get("sniper_use_live_price", False) and live_price > 0:
                print(f"[AutoTrader] 📡 Live price: {live_price} (señal era {price})", flush=True)

                # Max price cap — no comprar si live_price > max_buy_price
                max_buy = self._config.get("sniper_max_buy_price", 0.60)
                if live_price > max_buy:
                    error_msg = f"Live price {live_price} > max {max_buy}"
                    print(f"[AutoTrader] ❌ {error_msg}", flush=True)
                    return {"success": False, "error": error_msg}

                # Recalcular edge con live_price real Y descontando fee
                fair_odds = trade_info.get("fair_odds", 0)
                if fair_odds > 0:
                    # Fee por share (0 si es maker)
                    is_maker = self._config.get("sniper_use_maker", True)
                    fee = 0 if is_maker else _calc_taker_fee(live_price)
                    upside = max(1.0 - live_price, 0.01)
                    fee_pct = (fee / upside) * 100
                    raw_edge = round((fair_odds - live_price) / live_price * 100, 1)
                    net_edge = round(raw_edge - fee_pct, 1)
                    min_edge = self._config.get("min_edge", 5)
                    print(f"[AutoTrader] 📐 Edge: bruto={raw_edge}% fee={fee_pct:.1f}% "
                          f"neto={net_edge}% (fair={fair_odds} live={live_price} "
                          f"{'MAKER' if is_maker else 'TAKER'} min={min_edge}%)", flush=True)
                    if net_edge < min_edge:
                        error_msg = f"Edge neto {net_edge}% < min {min_edge}% (live={live_price})"
                        print(f"[AutoTrader] ❌ {error_msg}", flush=True)
                        return {"success": False, "error": error_msg}

                price = live_price
```

**Por qué**: La versión anterior calculaba edge como `(fair - live) / live`
sin descontar fees. La nueva versión:
- Usa max_buy_price configurable (no hardcoded 0.80)
- Calcula fee solo si es taker (maker = 0)
- Muestra edge bruto, fee%, y neto en los logs para debugging
- Filtra con el edge NETO

---

## PASO 10: Subir max_buy_price default y ajustar min_edge
**Archivo**: `src/config.py`
**Ubicación**: Línea 184
**Acción**: Cambiar default de max_buy_price

**ANTES**:
```python
CRYPTO_SNIPER_MAX_BUY_PRICE = _sniper_crypto.get("max_buy_price", 0.60)
```

**DESPUÉS**:
```python
CRYPTO_SNIPER_MAX_BUY_PRICE = _sniper_crypto.get("max_buy_price", 0.55)
```

**Por qué**: A $0.55, el upside potencial es $0.45/share (si gana). A $0.60
es $0.40/share. Bajar a 0.55 nos obliga a comprar solo cuando hay suficiente
upside. Con maker (0% fee) y 0.55, necesitamos 55% accuracy para break-even.

---

## PASO 11: Logging mejorado con info de fees
**Archivo**: `src/crypto_arb/autotrader.py`
**Ubicación**: Línea 382, el print de "PASS" en evaluate_signal
**Acción**: Agregar info de fees al log

**ANTES** (línea 382):
```python
        print(f"{tag} PASS [{strategy}]: edge={edge}% conf={confidence}% odds={poly_odds} remaining={remaining}s -> EXECUTING ${bet_size}", flush=True)
```

**DESPUÉS**:
```python
        is_maker = strategy == "sniper" and cfg.get("sniper_use_maker", True)
        fee_info = "MAKER(0%fee)" if is_maker else f"TAKER(~{fee_pct:.1f}%fee)"
        print(f"{tag} PASS [{strategy}] {fee_info}: net_edge={edge}% conf={confidence}% odds={poly_odds} remaining={remaining}s -> EXECUTING ${bet_size}", flush=True)
```

**Por qué**: Para que en los logs se vea claramente si el trade se ejecuta
como maker o taker y cuánto fee se está descontando.

---

## PASO 12: Agregar controles al Dashboard
**Archivo**: `src/dashboard/index.html`
**Ubicación**: En la sección de configuración sniper del dashboard
**Acción**: Agregar 2 nuevos controles

Agregar después del checkbox de `sniper_use_live_price`:

```html
<!-- Sniper Maker Mode -->
<label class="flex items-center gap-2">
    <input type="checkbox" id="cfg-sniper-use-maker" class="cfg-input"
           data-key="at_sniper_use_maker" checked>
    <span class="text-xs text-green-300">🏷️ Modo Maker (0% fee)</span>
    <span class="text-[10px] text-gray-500 ml-1">(postea GTC por debajo del ask en vez de FOK taker)</span>
</label>
<!-- Maker Offset -->
<div class="flex items-center gap-2">
    <label class="text-xs text-gray-400 w-32">Maker offset ($):</label>
    <input type="number" id="cfg-sniper-maker-offset" class="cfg-input w-20 bg-gray-800 text-white text-xs px-2 py-1 rounded"
           data-key="at_sniper_maker_offset" value="0.01" min="0.005" max="0.05" step="0.005">
    <span class="text-[10px] text-gray-500">(centavos por debajo del ask)</span>
</div>
```

**Por qué**: El usuario puede activar/desactivar modo maker y ajustar
el offset desde el dashboard sin tocar código.

---

## RESUMEN DE ARCHIVOS MODIFICADOS

| Archivo | Cambios | Líneas aprox |
|---------|---------|-------------|
| `src/crypto_arb/autotrader.py` | Función fee, método fee_rate, evaluate_signal fee-aware, maker pricing, config keys, logging | ~60 líneas nuevas, ~30 modificadas |
| `src/crypto_arb/detector.py` | fair_odds conservadores | ~10 líneas modificadas |
| `src/config.py` | max_buy_price default | 1 línea |
| `src/dashboard/index.html` | 2 controles nuevos (checkbox + input) | ~12 líneas nuevas |

---

## IMPACTO ESPERADO

### Antes (estado actual):
- **Fee por trade**: ~1.5% (taker FOK)
- **fair_odds**: 0.78-0.93 (optimistas)
- **Edge percibido**: 15-25% (ilusión)
- **Edge real**: ~5-8% después de fees y timing
- **Breakeven accuracy**: ~60%
- **Resultado**: PÉRDIDA

### Después (con estos fixes):
- **Fee por trade**: 0% (maker GTC)
- **fair_odds**: 0.58-0.82 (conservadores)
- **Edge percibido = Edge real**: sin fees ocultos
- **Breakeven accuracy**: ~55% (maker sin fee)
- **Resultado esperado**: BREAKEVEN a POSITIVO (si accuracy > 55%)

### Tabla de escenarios con maker a $0.50:
| Accuracy | EV por $5 trade | Resultado diario (20 trades) |
|----------|----------------|------------------------------|
| 50% | -$0.00 | $0.00 (breakeven) |
| 55% | +$0.50 | +$10.00 |
| 60% | +$1.00 | +$20.00 |
| 45% | -$0.50 | -$10.00 |

---

## ORDEN DE IMPLEMENTACIÓN

1. Paso 1 → función helper (sin impacto)
2. Paso 2 → método fee_rate (sin impacto)
3. Paso 8 → config keys nuevas (sin impacto)
4. Paso 4 → fair_odds conservadores (IMPACTO: menos señales pero más realistas)
5. Paso 3 → fee-aware evaluate_signal (IMPACTO: filtra trades sin edge real)
6. Paso 5 → sniper de FOK a GTC (IMPACTO MAYOR: 0% fee)
7. Paso 6 → pricing maker inteligente (IMPACTO MAYOR: compra más barato)
8. Paso 9 → edge con fee en execute_trade (IMPACTO: doble verificación)
9. Paso 7 → timeout ajustado (IMPACTO menor)
10. Paso 10 → config default (IMPACTO menor)
11. Paso 11 → logging mejorado (sin impacto funcional)
12. Paso 12 → dashboard controles (sin impacto funcional)

---

## RIESGOS Y MITIGACIÓN

| Riesgo | Mitigación |
|--------|-----------|
| Órdenes maker no se llenan | Timeout de 30s + cancelación automática. Si fill rate < 30%, subir offset |
| fair_odds demasiado bajos → 0 trades | Monitorear señales generadas. Si 0 en 2h, subir fair_odds 5% |
| Mercado se mueve mientras esperamos fill | El polling cada 5s detecta fill. Si no llena y precio subió, la orden maker está protegida (compramos más barato o no compramos) |
| Rebates no llegan | Los rebates son bonus, no la estrategia principal. La rentabilidad no depende de ellos |

---

## DESPUÉS DE IMPLEMENTAR: VERIFICACIÓN

1. Revisar logs buscando: `MAKER price`, `net_edge`, `TAKER`, `fee=`
2. Confirmar que NO aparezcan trades con `fee=0` cuando es taker
3. Confirmar que señales con edge < min_edge + fee son filtradas
4. Monitorear fill rate de órdenes maker durante 2-3 horas
5. Si fill rate es muy bajo (<20%), aumentar `sniper_maker_offset` a 0.02

---

> **¿Apruebas esta guía? Si sí, implemento todos los pasos en orden.**
