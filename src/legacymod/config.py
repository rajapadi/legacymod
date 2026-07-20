"""Configuration loading for legacymod.

Config lives in ``legacymod.toml`` (see ``legacymod.toml.example``).
Search order: explicit ``--config`` path, then ``./legacymod.toml``.
Missing file is fine — defaults apply. No API keys are ever read here;
provider config is names/flags only.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Config:
    """Resolved runtime configuration."""

    workspace: Path = Path("workspace")
    llm_provider: str = "stub"
    llm_model: str = "stub-v1"
    known_utilities: list[str] = field(default_factory=lambda: [
        # Vendor/system utilities that are legitimately absent from a
        # source library — referenced-but-missing checks skip these.
        "IKJEFT01", "IEFBR14", "IEBGENER", "IDCAMS", "SORT", "DFSORT",
        "SYNCSORT", "ICETOOL", "IEBCOPY", "DSNTIAD", "DSNTEP2", "FTP",
        "DMBATCH", "DGADBATC", "BPXBATCH", "XCOMJOB", "ADRDSSU", "IEHLIST",
        "CBLTDLI", "AIBTDLI", "DFSRRC00", "MQPUT", "MQPUT1", "MQGET",
        "MQOPEN", "MQCLOSE", "MQCONN", "MQDISC",
    ])
    config_path: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.workspace / "legacymod.db"


def load_config(config_path: str | Path | None = None,
                workspace_override: str | Path | None = None) -> Config:
    """Load config from TOML, applying CLI overrides.

    :param config_path: explicit path to a legacymod.toml, or None to
        look for ``./legacymod.toml``.
    :param workspace_override: ``--workspace`` CLI value, wins over TOML.
    """
    cfg = Config()
    path = Path(config_path) if config_path else Path("legacymod.toml")
    if path.is_file():
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        cfg.config_path = path
        if "workspace" in data:
            cfg.workspace = Path(str(data["workspace"]))
        llm = data.get("llm", {})
        if isinstance(llm, dict):
            cfg.llm_provider = str(llm.get("provider", cfg.llm_provider))
            cfg.llm_model = str(llm.get("model", cfg.llm_model))
        util = data.get("known_utilities")
        if isinstance(util, list):
            cfg.known_utilities = [str(u).upper() for u in util]
        log.debug("loaded config from %s", path)
    elif config_path:
        raise FileNotFoundError(f"config file not found: {path}")
    if workspace_override:
        cfg.workspace = Path(workspace_override)
    return cfg
