"""ML Scoring — Modelo de machine learning para scoring de alertas."""
import time
import math
from src import config


class MLScorer:
    """Scoring basado en regresión logística entrenado con datos históricos."""

    def __init__(self, db):
        self.db = db
        self._weights = {}  # feature_name -> weight
        self._bias = 0.0
        self._trained = False
        self._last_train = 0
        self._training_samples = 0
        self._accuracy = 0.0

    async def train(self):
        """Entrenar modelo con datos históricos de alertas resueltas."""
        if not config.FEATURE_ML_SCORING:
            return

        now = time.time()
        if self._trained and now - self._last_train < config.ML_RETRAIN_HOURS * 3600:
            return

        try:
            data = await self.db.get_ml_training_data()
            if not data or len(data) < config.ML_MIN_TRAINING_SAMPLES:
                print(f"ML: insuficientes muestras ({len(data or [])} < {config.ML_MIN_TRAINING_SAMPLES})", flush=True)
                return

            # Preparar features y labels
            features_list = []
            labels = []
            feature_names = [
                "score", "wallet_trades", "wallet_winrate", "market_volume",
                "size_usd", "num_triggers", "hour_of_day", "is_contrarian",
                "has_cluster", "is_accumulation",
            ]

            for row in data:
                f = self._extract_features(row, feature_names)
                features_list.append(f)
                labels.append(1.0 if row.get("was_correct") else 0.0)

            self._training_samples = len(features_list)

            # Entrenar regresión logística con gradient descent
            self._weights = {name: 0.0 for name in feature_names}
            self._bias = 0.0
            lr = 0.01
            epochs = 100

            for epoch in range(epochs):
                total_loss = 0
                correct = 0
                for i, features in enumerate(features_list):
                    # Forward
                    z = self._bias + sum(self._weights.get(k, 0) * v for k, v in features.items())
                    pred = self._sigmoid(z)
                    label = labels[i]

                    # Loss
                    eps = 1e-7
                    loss = -(label * math.log(pred + eps) + (1 - label) * math.log(1 - pred + eps))
                    total_loss += loss

                    # Accuracy
                    if (pred >= 0.5) == (label >= 0.5):
                        correct += 1

                    # Backward
                    error = pred - label
                    self._bias -= lr * error
                    for k, v in features.items():
                        self._weights[k] = self._weights.get(k, 0) - lr * error * v

                self._accuracy = correct / len(features_list) if features_list else 0

            self._trained = True
            self._last_train = now
            print(f"ML: entrenado con {self._training_samples} muestras, accuracy={self._accuracy:.1%}", flush=True)

        except Exception as e:
            print(f"Error entrenando ML: {e}", flush=True)

    def predict(self, alert_data: dict) -> float:
        """Predecir probabilidad de que una alerta sea correcta."""
        if not self._trained or not config.FEATURE_ML_SCORING:
            return 0.5

        feature_names = [
            "score", "wallet_trades", "wallet_winrate", "market_volume",
            "size_usd", "num_triggers", "hour_of_day", "is_contrarian",
            "has_cluster", "is_accumulation",
        ]
        features = self._extract_features(alert_data, feature_names)
        z = self._bias + sum(self._weights.get(k, 0) * v for k, v in features.items())
        return self._sigmoid(z)

    def get_blended_score(self, traditional_score: int, alert_data: dict, max_score: int) -> float:
        """Combinar score tradicional con ML según peso configurado."""
        if not config.FEATURE_ML_SCORING or not self._trained:
            return traditional_score

        ml_prob = self.predict(alert_data)
        ml_score = ml_prob * max_score

        w = config.ML_WEIGHT
        blended = (1 - w) * traditional_score + w * ml_score
        return round(blended, 1)

    def _extract_features(self, row: dict, feature_names: list) -> dict:
        """Extraer features normalizadas de una fila de datos."""
        features = {}
        for name in feature_names:
            if name == "score":
                features[name] = (row.get("score", 0) or 0) / 20.0
            elif name == "wallet_trades":
                features[name] = min((row.get("wallet_trades", 0) or 0) / 100.0, 1.0)
            elif name == "wallet_winrate":
                features[name] = (row.get("wallet_winrate", 0) or 0) / 100.0
            elif name == "market_volume":
                features[name] = min((row.get("market_volume", 0) or 0) / 1000000.0, 1.0)
            elif name == "size_usd":
                features[name] = min((row.get("size", 0) or 0) / 10000.0, 1.0)
            elif name == "num_triggers":
                triggers = row.get("triggers", "") or ""
                features[name] = len(triggers.split("||")) / 10.0 if triggers else 0
            elif name == "hour_of_day":
                features[name] = (row.get("hour", 12) or 12) / 24.0
            elif name == "is_contrarian":
                features[name] = 1.0 if "contrarian" in (row.get("triggers", "") or "").lower() else 0
            elif name == "has_cluster":
                features[name] = 1.0 if "cluster" in (row.get("triggers", "") or "").lower() else 0
            elif name == "is_accumulation":
                features[name] = 1.0 if "accumul" in (row.get("triggers", "") or "").lower() else 0
            else:
                features[name] = 0.0
        return features

    @staticmethod
    def _sigmoid(z: float) -> float:
        z = max(min(z, 500), -500)
        return 1.0 / (1.0 + math.exp(-z))

    def get_stats(self) -> dict:
        return {
            "enabled": config.FEATURE_ML_SCORING,
            "trained": self._trained,
            "training_samples": self._training_samples,
            "accuracy": round(self._accuracy * 100, 1),
            "weight": config.ML_WEIGHT,
            "top_features": dict(sorted(self._weights.items(), key=lambda x: abs(x[1]), reverse=True)[:5]),
        }
