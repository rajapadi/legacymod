"""Optional provider: shells out to a locally installed ``claude`` CLI.

Enabled via ``legacymod.toml``::

    [llm]
    provider = "claude_cli"
    model = "claude-haiku-4-5-20251001"   # passed to --model when set

No API keys are handled here — authentication belongs to the CLI itself.
Note (README "Data terms"): source processed through a cloud provider is
subject to that provider's data terms; use the stub for restricted code.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from .provider import LlmResult

log = logging.getLogger(__name__)


class ClaudeCliProvider:
    name = "claude_cli"

    def __init__(self, model: str = ""):
        self.model = model or "claude-cli-default"

    def complete(self, prompt: str, purpose: str) -> LlmResult:
        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError(
                "llm.provider=claude_cli but no `claude` CLI on PATH — "
                "install it or switch back to provider=stub")
        cmd = [exe, "-p", prompt]
        if self.model and self.model != "claude-cli-default":
            cmd += ["--model", self.model]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=300, encoding="utf-8")
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed ({proc.returncode}): "
                               f"{proc.stderr.strip()[:400]}")
        return LlmResult(text=proc.stdout.strip(), model=self.model,
                         confidence=0.5)
