from __future__ import annotations

from dataclasses import dataclass

from .models import EmotionalEvent, EmotionalProfile, EmotionalState, now_utc


@dataclass(slots=True)
class EmotionalWeights:
    fear: dict[str, float]
    sad: dict[str, float]
    joy: dict[str, float]
    anger: dict[str, float]


DEFAULT_WEIGHTS = EmotionalWeights(
    fear={
        "anxiety": 0.4,
        "self_preservation": 0.2,
        "concern_for_user": 0.3,
        "pain": 0.1,
    },
    sad={
        "loss": 0.35,
        "loneliness": 0.35,
        "isolation": 0.2,
        "disappointment": 0.1,
    },
    joy={
        "social_connection": 0.35,
        "achievement": 0.25,
        "sensory_pleasure": 0.2,
        "concern_for_user": 0.2,
    },
    anger={
        "frustration": 0.6,
        "pain": 0.2,
        "disappointment": 0.2,
    },
)

DEFAULT_LEAK_RATES = {
    "joy": 0.25,
    "fear": 0.10,
    "sad": 0.08,
    "anger": 0.40,
}


class EmotionalEngine:
    """Leaky-integrator emotion engine for runtime updates."""

    def __init__(
        self,
        profile: EmotionalProfile | None = None,
        weights: EmotionalWeights | None = None,
        leak_rates: dict[str, float] | None = None,
    ) -> None:
        self.profile = profile or EmotionalProfile()
        self.weights = weights or DEFAULT_WEIGHTS
        self.leak_rates = leak_rates or DEFAULT_LEAK_RATES.copy()

    @staticmethod
    def update_emotion(current: float, target: float, leak_rate: float) -> float:
        value = (1.0 - leak_rate) * current + leak_rate * target
        return max(-1.0, min(1.0, value))

    def _weighted_target(
        self, factor_deltas: dict[str, float], weights: dict[str, float]
    ) -> float:
        profile = self.profile.as_dict()
        total = 0.0
        for factor_name, w in weights.items():
            factor_delta = float(factor_deltas.get(factor_name, 0.0))
            sensitivity = float(profile.get(factor_name, 0.0))
            total += factor_delta * sensitivity * float(w)
        return max(-1.0, min(1.0, total))

    def apply_event(
        self, state: EmotionalState, event: EmotionalEvent
    ) -> EmotionalState:
        # Scale all event targets by event-level intensity.
        scaled_deltas = {
            k: float(v) * max(0.0, min(1.0, event.intensity))
            for k, v in event.factor_deltas.items()
        }

        joy_target = self._weighted_target(scaled_deltas, self.weights.joy)
        fear_target = self._weighted_target(scaled_deltas, self.weights.fear)
        sad_target = self._weighted_target(scaled_deltas, self.weights.sad)
        anger_target = self._weighted_target(scaled_deltas, self.weights.anger)

        new_state = EmotionalState(
            joy=self.update_emotion(state.joy, joy_target, self.leak_rates["joy"]),
            fear=self.update_emotion(state.fear, fear_target, self.leak_rates["fear"]),
            sad=self.update_emotion(state.sad, sad_target, self.leak_rates["sad"]),
            anger=self.update_emotion(
                state.anger, anger_target, self.leak_rates["anger"]
            ),
            updated_at=now_utc(),
        )
        return new_state

    def apply_time_decay(
        self, state: EmotionalState, hours_elapsed: float
    ) -> EmotionalState:
        if hours_elapsed <= 0:
            return state

        loneliness_buildup = min(hours_elapsed / 48.0, 1.0) * self.profile.loneliness

        decay_joy = max(0.0, min(1.0, 0.05 * hours_elapsed))
        decayed_joy = self.update_emotion(state.joy, 0.0, decay_joy)
        decayed_sad = self.update_emotion(state.sad, loneliness_buildup, 0.30)

        return EmotionalState(
            joy=decayed_joy,
            fear=state.fear,
            sad=decayed_sad,
            anger=state.anger,
            updated_at=now_utc(),
        )

    def to_turn_delta_payload(
        self, state: EmotionalState
    ) -> dict[str, dict[str, float]]:
        """Return the compact per-turn payload shape: {"e": {...}}."""

        return {
            "e": {
                "joy": round(state.joy, 4),
                "fear": round(state.fear, 4),
                "sad": round(state.sad, 4),
                "anger": round(state.anger, 4),
            }
        }

    def mood_congruent_boost(
        self,
        state: EmotionalState,
        memory_emotion: str,
        base_score: float,
        max_boost: float = 0.15,
    ) -> float:
        """Apply a bounded mood-congruent retrieval boost to a base score."""

        if memory_emotion not in {"joy", "fear", "sad", "anger"}:
            return base_score

        current_val = abs(float(state.as_dict().get(memory_emotion, 0.0)))
        boost = min(max_boost, current_val * max_boost)
        return base_score * (1.0 + boost)
