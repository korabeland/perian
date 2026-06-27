"""Single indirection point for the trust spine's external-world anchors.

Perian x ATB merge, U13 (Phase A). Every hardcoded ~/.hermes / ~/.claude /
~/.lmstudio absolute path the spine depended on now resolves through here, so
the spine can relocate into the monorepo with behaviour unchanged. Each anchor
keeps its exact current default and gains an env override; nothing is moved --
the indirection just makes the monorepo<->external-world boundary explicit and
overridable.

RESOLVED LAZILY (functions, not module constants): each accessor evaluates
Path.home() / the env var at CALL time, exactly mirroring the original
in-function `Path.home() / "..."` expressions it replaces. So a monkeypatched
Path.home (tests) or a runtime env change is honoured -- and a module-level
caller that assigns `X = paths.hermes_scripts()` at import gets the same
import-time value the original module-level constant had. (An eager module
constant here would silently move resolution to import time and break both.)

The boundary (what stays external to the future monorepo):
  - Hermes install: profiles, scripts, logs, app root      (hermes_*)
  - User launcher + uv-managed interpreter                 (local_bin, uv_data)
  - Claude memory dir (grounding pack reads agent memory)  (claude_memory_dir)
  - Model-backend CLI: LM Studio today, Ollama after U2    (lms_bin)
  - gbrain CLI: already env-resolved at its single site in tools/brain_digest.py
    (GBRAIN_BIN) -- left there, noted here for the complete boundary map.

Imported as `from config import paths`: the repo root is on sys.path in every
runtime context (scripts run `python -m tools.X` from the root; tests run
`python -m pytest` from the root) -- the same requirement that already makes
`from tools import X` resolve.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_path(var: str, default: Path) -> Path:
    """External anchor: env override if set, else the historical default."""
    override = os.environ.get(var)
    return Path(override).expanduser() if override else default


# --- Hermes install (stays external to the monorepo) ---
def hermes_home() -> Path:
    return _env_path("HERMES_HOME", Path.home() / ".hermes")


def hermes_profiles() -> Path:
    return _env_path("HERMES_PROFILES", hermes_home() / "profiles")


def hermes_scripts() -> Path:
    return _env_path("HERMES_SCRIPTS", hermes_home() / "scripts")


def hermes_logs() -> Path:
    return _env_path("HERMES_LOGS", hermes_home() / "logs")


def hermes_app_root() -> Path:
    return _env_path("HERMES_APP_ROOT", hermes_home() / "hermes-agent")


# --- User-level launcher + uv interpreter (think_sweep sandbox allowlist) ---
def local_bin() -> Path:
    return _env_path("HERMES_LOCAL_BIN", Path.home() / ".local" / "bin")


def uv_data() -> Path:
    return _env_path("HERMES_UV_DATA", Path.home() / ".local" / "share" / "uv")


# --- Claude memory dir (grounding_pack reads agent memory from here) ---
def claude_memory_dir() -> Path:
    return _env_path(
        "CLAUDE_MEMORY_DIR",
        Path.home() / ".claude" / "projects" / "perian" / "memory",
    )


# --- Model-backend CLI (LM Studio; retained for rollback / non-Ollama paths) ---
def lms_bin() -> Path:
    return _env_path("LMS_BIN", Path.home() / ".lmstudio" / "bin" / "lms")


# --- Model-backend inference + residency endpoint (U2: LM Studio :1234 -> Ollama :11434) ---
def ollama_base_url() -> str:
    """Ollama server root. Native /api/* (residency) and /v1/* (inference) hang
    off this. URL string (not a Path); OLLAMA_BASE_URL overrides the default."""
    return os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
