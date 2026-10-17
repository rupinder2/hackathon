from __future__ import annotations

import threading
from typing import Any


class Usage:
    """Thread-safe tally of the tokens, cost and phone time one search run consumed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.openai = {"calls": 0, "input": 0, "output": 0}
        self.voice = {"calls": 0, "seconds": 0, "input": 0, "cached": 0, "output": 0, "cost_usd": 0.0}

    def add_openai(self, response: dict[str, Any]) -> None:
        """Chat Completions response body: its `usage` block has prompt_tokens / completion_tokens."""
        u = response.get("usage") or {}
        with self._lock:
            self.openai["calls"] += 1
            self.openai["input"] += int(u.get("prompt_tokens") or 0)
            self.openai["output"] += int(u.get("completion_tokens") or 0)

    def add_conversation(self, data: dict[str, Any] | None) -> None:
        """One ElevenLabs conversation payload: duration, LLM tokens and cost live under `metadata`."""
        meta = (data or {}).get("metadata") or {}
        if not meta:  # the call never started (API error, not configured): nothing was consumed
            return
        charging = meta.get("charging") or {}
        # "irreversible" is what the agent actually generated and was billed for
        by_model = ((charging.get("llm_usage") or {}).get("irreversible_generation") or {}).get("model_usage") or {}
        with self._lock:
            self.voice["calls"] += 1
            self.voice["seconds"] += int(meta.get("call_duration_secs") or 0)
            self.voice["cost_usd"] += float(meta.get("cost_fiat") or 0)
            for m in by_model.values():
                self.voice["input"] += int((m.get("input") or {}).get("tokens") or 0)
                self.voice["cached"] += int((m.get("input_cache_read") or {}).get("tokens") or 0)
                self.voice["output"] += int((m.get("output_total") or {}).get("tokens") or 0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            o, v = dict(self.openai), dict(self.voice)
        return {
            "total_tokens": o["input"] + o["output"] + v["input"] + v["cached"] + v["output"],
            "openai": o,
            "voice": {**v, "cost_usd": round(v["cost_usd"], 4)},
        }
