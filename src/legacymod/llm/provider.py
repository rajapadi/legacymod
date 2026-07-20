"""LLM provider protocol + cached, logged completion.

Rules that apply to every provider (enforced here, not per-provider):

- Every call is logged to the ``llm_log`` table and mirrored to
  ``workspace/llm_log.csv`` (timestamp, provider, model, purpose,
  artifact, prompt/response SHA-256, accepted_by_human, cache_hit).
- Responses are cached by prompt hash — re-runs are deterministic and
  cost nothing.
- No API keys are read anywhere; the ``claude_cli`` provider shells out
  to a locally installed CLI, the ``stub`` provider is pure Python.
- Callers must pass an explicit ``--enrich``/``--llm-impl`` flag; there
  is no silent LLM use.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from ..config import Config
from ..store import Store

log = logging.getLogger(__name__)


@dataclass
class LlmResult:
    text: str
    model: str
    confidence: float = 0.5


class LlmProvider(Protocol):
    name: str

    def complete(self, prompt: str, purpose: str) -> LlmResult: ...


def get_provider(cfg: Config) -> LlmProvider:
    if cfg.llm_provider == "stub":
        from .stub import StubProvider
        return StubProvider(cfg.llm_model)
    if cfg.llm_provider == "claude_cli":
        from .claude_cli import ClaudeCliProvider
        return ClaudeCliProvider(cfg.llm_model)
    raise ValueError(f"unknown llm provider {cfg.llm_provider!r} "
                     "(expected 'stub' or 'claude_cli')")


def complete_cached(store: Store, cfg: Config, prompt: str, purpose: str,
                    artifact: str) -> tuple[LlmResult, bool]:
    """Cached completion; logs every call. Returns (result, cache_hit)."""
    provider = get_provider(cfg)
    phash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    row = store.query("SELECT response, model FROM llm_cache"
                      " WHERE prompt_sha256=?", (phash,))
    if row:
        result = LlmResult(row[0]["response"], row[0]["model"])
        hit = True
    else:
        result = provider.complete(prompt, purpose)
        store.execute(
            "INSERT INTO llm_cache VALUES (?,?,?,?,datetime('now'))",
            (phash, provider.name, result.model, result.text))
        hit = False
    store.execute(
        "INSERT INTO llm_log VALUES (?,?,?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),
         provider.name, result.model, purpose, artifact, phash,
         hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
         0, 1 if hit else 0))
    export_log(store, cfg)
    return result, hit


def export_log(store: Store, cfg: Config) -> None:
    store.export_csv(
        "SELECT timestamp, provider, model, purpose, artifact, prompt_sha256,"
        " response_sha256, accepted_by_human, cache_hit FROM llm_log"
        " ORDER BY timestamp", cfg.workspace / "llm_log.csv")


def ai_block(result: LlmResult, provider_name: str) -> str:
    """The visible marking wrapped around every piece of AI content."""
    stamp = datetime.now(timezone.utc).date().isoformat()
    head = (f"> **AI-generated** (provider={provider_name}, "
            f"model={result.model}, {stamp}, "
            f"confidence={result.confidence:.2f}, needs_review)")
    body = "\n".join("> " + l for l in result.text.splitlines())
    return head + "\n>\n" + body
