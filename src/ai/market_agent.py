"""AI Market Agent — Analizar mercados con LLM para detectar oportunidades."""
import os
import re
import time
import httpx
from src import config


class MarketAgent:
    """Agente AI que analiza mercados y estima probabilidades."""

    def __init__(self, db):
        self.db = db
        self._cache = {}  # market_id -> {result, ts}
        self._api_key = os.getenv("OPENAI_API_KEY", "")
        self._model = os.getenv("AI_MODEL", "gpt-4o-mini")
        self._last_batch = 0
        self._batch_interval = 3600  # 1h entre análisis batch

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def analyze_market(self, market: dict, news: list = None, whale_activity: list = None) -> dict:
        """Analizar un mercado individual con AI.

        market: {condition_id, question, price, volume, end_date, category}
        news: lista de {title, source, published_at}
        whale_activity: lista de {side, size, wallet_address}

        Retorna: {ai_probability, edge_pct, reasoning, confidence}
        """
        if not self._api_key:
            return {"error": "No API key configurada", "ai_probability": None}

        market_id = market.get("condition_id", "")

        # Cache de 1h por mercado
        now = time.time()
        if market_id in self._cache and now - self._cache[market_id]["ts"] < 3600:
            return self._cache[market_id]["result"]

        prompt = self._build_prompt(market, news, whale_activity)

        try:
            result = await self._call_llm(prompt)
            parsed = self._parse_response(result, market)

            # Guardar en cache y DB
            self._cache[market_id] = {"result": parsed, "ts": now}
            await self.db.save_ai_analysis(
                market_id=market_id,
                market_question=market.get("question", ""),
                ai_probability=parsed.get("ai_probability"),
                market_price=market.get("price", 0),
                edge_pct=parsed.get("edge_pct", 0),
                reasoning=parsed.get("reasoning", ""),
                model=self._model,
            )

            return parsed
        except Exception as e:
            print(f"[AI Agent] Error analizando {market.get('question', '')[:50]}: {e}", flush=True)
            return {"error": str(e), "ai_probability": None}

    async def batch_analyze(self, markets: list, news_by_market: dict = None) -> list:
        """Analizar batch de mercados (top por volumen)."""
        now = time.time()
        if now - self._last_batch < self._batch_interval:
            return []

        self._last_batch = now
        results = []

        # Ordenar por volumen y tomar top 20
        sorted_markets = sorted(markets, key=lambda m: m.get("volume", 0), reverse=True)[:20]

        for market in sorted_markets:
            mid = market.get("condition_id", "")
            news = (news_by_market or {}).get(mid, [])
            result = await self.analyze_market(market, news=news)
            if result.get("ai_probability") is not None:
                result["market_id"] = mid
                result["market_question"] = market.get("question", "")
                results.append(result)

        if results:
            print(f"[AI Agent] Batch: {len(results)} mercados analizados", flush=True)

        return results

    def _build_prompt(self, market: dict, news: list = None, whale_activity: list = None) -> str:
        """Construir prompt para el LLM."""
        question = market.get("question", "")
        price = market.get("price", 0)
        volume = market.get("volume", 0)
        category = market.get("category", "")
        end_date = market.get("end_date", "")

        prompt = f"""Eres un analista de mercados de predicción. Analiza este mercado de Polymarket y estima la probabilidad real del evento.

MERCADO: {question}
PRECIO ACTUAL: {price:.2f} (el mercado dice {price*100:.0f}% probabilidad de YES)
VOLUMEN 24H: ${volume:,.0f}
CATEGORÍA: {category}
FECHA CIERRE: {end_date}
"""

        if news:
            prompt += "\nNOTICIAS RECIENTES:\n"
            for n in news[:5]:
                prompt += f"- {n.get('title', '')} ({n.get('source', '')})\n"

        if whale_activity:
            prompt += "\nACTIVIDAD DE BALLENAS (trades >$50K):\n"
            buys = sum(1 for w in whale_activity if w.get("side") == "BUY")
            sells = len(whale_activity) - buys
            total_vol = sum(w.get("size", 0) for w in whale_activity)
            prompt += f"- {buys} compras, {sells} ventas, volumen total: ${total_vol:,.0f}\n"

        prompt += """
INSTRUCCIONES:
1. Estima la probabilidad REAL de que el evento ocurra (0.00 a 1.00)
2. Compara con el precio actual del mercado
3. Si hay diferencia significativa (>10%), explica por qué

RESPONDE EXACTAMENTE en este formato (sin markdown):
PROBABILIDAD: 0.XX
CONFIANZA: alta/media/baja
EDGE: +X.X% o -X.X%
RAZONAMIENTO: [Tu análisis en 2-3 frases]
"""
        return prompt

    async def _call_llm(self, prompt: str) -> str:
        """Llamar al LLM (OpenAI compatible)."""
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 300,
                },
            )
            if r.status_code != 200:
                raise Exception(f"LLM API error {r.status_code}: {r.text[:200]}")

            data = r.json()
            return data["choices"][0]["message"]["content"]

    def _parse_response(self, text: str, market: dict) -> dict:
        """Parsear respuesta del LLM."""
        result = {
            "ai_probability": None,
            "confidence": "baja",
            "edge_pct": 0,
            "reasoning": "",
        }

        # Buscar PROBABILIDAD
        prob_match = re.search(r'PROBABILIDAD:\s*([0-9.]+)', text, re.IGNORECASE)
        if prob_match:
            try:
                prob = float(prob_match.group(1))
                if 0 <= prob <= 1:
                    result["ai_probability"] = round(prob, 3)
            except ValueError:
                pass

        # Buscar CONFIANZA
        conf_match = re.search(r'CONFIANZA:\s*(\w+)', text, re.IGNORECASE)
        if conf_match:
            result["confidence"] = conf_match.group(1).lower()

        # Buscar EDGE
        edge_match = re.search(r'EDGE:\s*([+-]?[0-9.]+)', text, re.IGNORECASE)
        if edge_match:
            try:
                result["edge_pct"] = round(float(edge_match.group(1)), 1)
            except ValueError:
                pass

        # Calcular edge si no viene
        market_price = market.get("price", 0)
        if result["ai_probability"] is not None and result["edge_pct"] == 0 and market_price > 0:
            result["edge_pct"] = round((result["ai_probability"] - market_price) * 100, 1)

        # Buscar RAZONAMIENTO
        reason_match = re.search(r'RAZONAMIENTO:\s*(.+)', text, re.IGNORECASE | re.DOTALL)
        if reason_match:
            result["reasoning"] = reason_match.group(1).strip()[:500]

        return result

    def get_stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "model": self._model,
            "cached_analyses": len(self._cache),
            "last_batch_ago": round(time.time() - self._last_batch) if self._last_batch else None,
        }
