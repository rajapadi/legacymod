"""Default LLM provider: deterministic, offline, clearly marked.

Returns canned placeholder analyses derived only from the prompt hash and
a light scan of the prompt text, so the entire pipeline and test suite
run with zero network and zero API keys. Output is unmistakably labeled
as a placeholder — it can never be confused with a real analysis.
"""

from __future__ import annotations

import hashlib
import re

from .provider import LlmResult


class StubProvider:
    name = "stub"

    def __init__(self, model: str = "stub-v1"):
        self.model = model or "stub-v1"

    def complete(self, prompt: str, purpose: str) -> LlmResult:
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        verbs = sorted(set(re.findall(
            r"\b(COMPUTE|MOVE|IF|CALL|WRITE|INSERT|UPDATE|DELETE|PERFORM)\b",
            prompt.upper())))
        seen = ", ".join(verbs) if verbs else "no recognized COBOL verbs"
        text = (f"[STUB PLACEHOLDER - not a real analysis] purpose={purpose}; "
                f"input digest {digest}; statements seen: {seen}. "
                "Configure llm.provider=claude_cli in legacymod.toml for a "
                "real explanation. This text is deterministic and offline.")
        return LlmResult(text=text, model=self.model, confidence=0.1)
