"""Copy Trading Engine — Simular copy trading de ballenas seleccionadas."""
import time


class CopyTradingEngine:
    """Motor de copy trading simulado para wallets de ballenas."""

    def __init__(self, db):
        self.db = db
        self._targets_cache = {}  # wallet -> config
        self._last_refresh = 0

    async def refresh_targets(self):
        """Refrescar lista de wallets objetivo desde DB."""
        now = time.time()
        if now - self._last_refresh < 60:
            return
        self._last_refresh = now
        try:
            targets = await self.db.get_copy_targets()
            self._targets_cache = {t["wallet_address"]: t for t in targets if t.get("enabled")}
        except Exception as e:
            print(f"[CopyTrading] Error refrescando targets: {e}", flush=True)

    async def process_whale_trade(self, trade) -> dict:
        """Evaluar si un whale trade debe ser copiado.

        trade: objeto Trade o dict con wallet_address, market_id, side, outcome, size, price
        Retorna: {copied: bool, copy_trade_id: int, ...} o {copied: False}
        """
        await self.refresh_targets()

        wallet = trade.wallet_address if hasattr(trade, 'wallet_address') else trade.get("wallet_address", "")
        if not wallet or wallet not in self._targets_cache:
            return {"copied": False}

        target = self._targets_cache[wallet]
        if not target.get("enabled", False):
            return {"copied": False}

        # Calcular tamaño simulado
        trade_size = trade.size if hasattr(trade, 'size') else trade.get("size", 0)
        scale_pct = target.get("scale_pct", 1.0)  # % del tamaño original
        max_per_trade = target.get("max_per_trade", 100)
        sim_size = min(trade_size * (scale_pct / 100), max_per_trade)

        if sim_size < 1:
            return {"copied": False}

        # Guardar copy trade en DB
        trade_data = {
            "target_wallet": wallet,
            "market_id": trade.market_id if hasattr(trade, 'market_id') else trade.get("market_id", ""),
            "market_question": trade.market_question if hasattr(trade, 'market_question') else trade.get("market_question", ""),
            "market_slug": trade.market_slug if hasattr(trade, 'market_slug') else trade.get("market_slug", ""),
            "side": trade.side if hasattr(trade, 'side') else trade.get("side", ""),
            "outcome": trade.outcome if hasattr(trade, 'outcome') else trade.get("outcome", ""),
            "original_size": trade_size,
            "sim_size": round(sim_size, 2),
            "entry_price": trade.price if hasattr(trade, 'price') else trade.get("price", 0),
        }

        try:
            copy_id = await self.db.save_copy_trade(trade_data)
            if copy_id:
                target_name = target.get("wallet_name") or wallet[:10]
                print(f"[CopyTrading] Copiado trade de {target_name}: "
                      f"${trade_size:,.0f} → sim ${sim_size:,.0f} "
                      f"({trade_data['side']} {trade_data['outcome']})", flush=True)
                return {"copied": True, "copy_trade_id": copy_id, "sim_size": sim_size}
        except Exception as e:
            print(f"[CopyTrading] Error guardando copy trade: {e}", flush=True)

        return {"copied": False}

    async def update_prices(self, price_getter):
        """Actualizar precios de copy trades abiertos (mark-to-market).

        price_getter: async callable(market_id, outcome) -> float
        """
        try:
            open_trades = await self.db.get_open_copy_trades()
            if not open_trades:
                return

            updated = 0
            for ct in open_trades:
                try:
                    price = await price_getter(ct["market_id"], ct.get("outcome", "Yes"))
                    if price is not None:
                        await self.db.update_copy_trade_price(ct["id"], price)
                        updated += 1
                except Exception:
                    pass

            if updated:
                print(f"[CopyTrading] Actualizado precios de {updated}/{len(open_trades)} copy trades", flush=True)
        except Exception as e:
            print(f"[CopyTrading] Error en update_prices: {e}", flush=True)

    def get_stats(self) -> dict:
        return {
            "active_targets": len(self._targets_cache),
            "target_wallets": list(self._targets_cache.keys())[:10],
        }
