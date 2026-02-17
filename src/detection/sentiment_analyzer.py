"""Sentiment Analyzer — Análisis de sentimiento de noticias por mercado."""
import re
from collections import defaultdict


class SentimentAnalyzer:
    """Analiza sentimiento de noticias y correlaciona con precios de mercado."""

    # Palabras clave para sentimiento positivo/negativo
    POSITIVE_WORDS = {
        "win", "wins", "winning", "won", "surge", "surges", "rally", "soar",
        "rise", "rises", "rising", "gain", "gains", "boost", "jump", "jumps",
        "up", "higher", "high", "record", "success", "approve", "approved",
        "pass", "passed", "agree", "agreement", "deal", "positive", "bullish",
        "strong", "strength", "growth", "grow", "increase", "support", "lead",
        "leading", "ahead", "favored", "likely", "confirm", "confirmed",
        "breakthrough", "victory", "elected", "launch", "launched",
    }
    NEGATIVE_WORDS = {
        "lose", "loses", "losing", "lost", "crash", "crashes", "fall", "falls",
        "drop", "drops", "decline", "plunge", "down", "lower", "low", "fail",
        "fails", "failed", "reject", "rejected", "deny", "denied", "negative",
        "bearish", "weak", "weakness", "decrease", "risk", "threat", "crisis",
        "collapse", "ban", "banned", "suspend", "suspended", "delay", "delayed",
        "unlikely", "doubt", "concern", "warning", "defeat", "defeated",
        "cancel", "cancelled", "withdraw", "withdrawn", "oppose", "opposition",
    }

    def __init__(self):
        self._market_sentiment = defaultdict(lambda: {"positive": 0, "negative": 0, "neutral": 0, "articles": 0})

    def analyze_article(self, title: str) -> dict:
        """Analizar sentimiento de un título de noticia.

        Retorna: {score: -100..+100, label: positive/negative/neutral}
        """
        if not title:
            return {"score": 0, "label": "neutral"}

        words = set(re.findall(r'\w+', title.lower()))
        pos_count = len(words & self.POSITIVE_WORDS)
        neg_count = len(words & self.NEGATIVE_WORDS)

        total = pos_count + neg_count
        if total == 0:
            return {"score": 0, "label": "neutral"}

        # Score: -100 a +100
        score = round(((pos_count - neg_count) / total) * 100)

        if score > 15:
            label = "positive"
        elif score < -15:
            label = "negative"
        else:
            label = "neutral"

        return {"score": score, "label": label}

    def analyze_market_news(self, articles: list) -> dict:
        """Analizar sentimiento agregado de noticias para un mercado.

        articles: lista de {title, ...}
        Retorna: {score, label, mention_count, positive, negative, neutral}
        """
        if not articles:
            return {
                "score": 0, "label": "neutral",
                "mention_count": 0, "positive": 0, "negative": 0, "neutral": 0,
            }

        scores = []
        pos = neg = neu = 0
        for article in articles:
            result = self.analyze_article(article.get("title", ""))
            scores.append(result["score"])
            if result["label"] == "positive":
                pos += 1
            elif result["label"] == "negative":
                neg += 1
            else:
                neu += 1

        avg_score = round(sum(scores) / len(scores)) if scores else 0

        if avg_score > 15:
            label = "positive"
        elif avg_score < -15:
            label = "negative"
        else:
            label = "neutral"

        return {
            "score": avg_score,
            "label": label,
            "mention_count": len(articles),
            "positive": pos,
            "negative": neg,
            "neutral": neu,
        }

    def detect_sentiment_spike(self, current_count: int, avg_count: float) -> bool:
        """Detectar si hay un spike de menciones vs promedio."""
        if avg_count <= 0:
            return current_count >= 5
        return current_count >= avg_count * 2  # 2x el promedio = spike

    def get_opportunity_signal(self, sentiment_score: int, price_change_pct: float) -> dict:
        """Detectar oportunidades contrarias (sentimiento vs precio).

        Si sentimiento positivo pero precio bajó → posible oportunidad de compra.
        Si sentimiento negativo pero precio subió → posible oportunidad de venta.
        """
        signal = {"has_signal": False, "type": None, "reason": ""}

        if abs(sentiment_score) < 20 or abs(price_change_pct) < 3:
            return signal

        # Divergencia: sentimiento y precio van en direcciones opuestas
        if sentiment_score > 30 and price_change_pct < -3:
            signal = {
                "has_signal": True,
                "type": "bullish_divergence",
                "reason": f"Noticias positivas ({sentiment_score:+d}) pero precio cayó {price_change_pct:.1f}%",
            }
        elif sentiment_score < -30 and price_change_pct > 3:
            signal = {
                "has_signal": True,
                "type": "bearish_divergence",
                "reason": f"Noticias negativas ({sentiment_score:+d}) pero precio subió +{price_change_pct:.1f}%",
            }

        return signal
