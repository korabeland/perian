"""F0a — swap-verify smoke test for the QA layer.

Validates the runtime path the QA runner depends on:

  1. `tools.swap_orchestrator qa` loads the model declared in
     ~/.hermes/profiles/qa/config.yaml (currently `gemma-4-26b-a4b-it`).
  2. `hermes -p qa chat -q <prompt> -Q --max-turns N` actually invokes the QA
     profile with that loaded model.
  3. The response comes from Gemma (not the prior Qwen, not gpt-oss).

This is the F0a prerequisite from the 2026-06-07 handoff. If it fails, the rest
of the QA layer is dead code — no point authoring F0b/F1/F2 until F0a is green.

CAUTION
-------
This makes a real model call. The swap WILL evict whatever LM Studio currently
has loaded. Pause the gate-sweep cron before running:

    hermes cron pause 09a24f272468        # ATB: Gate Sweep

Exit codes
----------
  0   Gemma confirmed in response.
  1   swap_orchestrator failed (model not loaded).
  2   hermes -p qa chat invocation failed.
  3   response did not contain the expected 'gemma' substring
      (see rival_tokens_detected in the JSON for diagnostic info).

Output
------
Always prints a single JSON object on stdout, even on error.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

QA_PROFILE = "qa"  # positional arg to swap_orchestrator; reads model.default from config.yaml
EXPECTED_GEMMA_MODEL = "gemma-4-26b-a4b-it"  # informational; not enforced
PROMPT = "Reply in one short sentence: what model are you?"
EXPECTED_TOKEN = "gemma"  # case-insensitive substring on stdout
# Specific rival tokens — designed to NOT false-positive on common English
# (e.g. plain 'gpt' would match 'I am not GPT'; 'phi' would match 'philosophy').
# When EXPECTED_TOKEN matches we don't care about rivals; when it doesn't,
# these tell us whether the swap silently no-op'd.
RIVAL_TOKENS = ("qwen", "gpt-oss", "llama", "mistral", "claude", "phi-")
SWAP_TIMEOUT_SECONDS = 180
INVOKE_TIMEOUT_SECONDS = 300  # generous: first cold load can be slow
MAX_TURNS = "3"  # 1 LLM turn + 2 spare in case the agent calls a tool first

ATB_ROOT = Path(__file__).resolve().parent.parent


def _emit(payload: dict, exit_code: int) -> None:
    print(json.dumps(payload, indent=2))
    sys.exit(exit_code)


def main() -> None:
    started = time.time()
    result: dict = {
        "swap_ok": False,
        "swap_exit": -1,
        "swap_stdout_tail": "",
        "swap_stderr_tail": "",
        "qa_profile": QA_PROFILE,
        "expected_gemma_model": EXPECTED_GEMMA_MODEL,
        "invoke_ok": False,
        "invoke_exit": -1,
        "response_excerpt": "",
        "response_says_gemma": False,
        "rival_tokens_detected": [],
        "timed_out": "",  # "" | "swap" | "invoke"
        "elapsed_seconds": 0.0,
        "error": "",
    }

    # Step 1: swap to Gemma via the existing swap_orchestrator (positional profile arg).
    try:
        swap = subprocess.run(
            [
                "python3",
                "-m",
                "tools.swap_orchestrator",
                QA_PROFILE,
            ],
            cwd=str(ATB_ROOT),
            capture_output=True,
            text=True,
            timeout=SWAP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        result["timed_out"] = "swap"
        result["error"] = f"swap timed out after {SWAP_TIMEOUT_SECONDS}s"
        result["elapsed_seconds"] = round(time.time() - started, 2)
        _emit(result, 1)
        return
    except OSError as e:
        result["error"] = f"swap orchestrator launch failed: {e}"
        result["elapsed_seconds"] = round(time.time() - started, 2)
        _emit(result, 1)
        return

    result["swap_exit"] = swap.returncode
    # Capture BOTH streams — swap_orchestrator runs `lms load` with capture=False
    # internally, so it may emit useful context on either stdout or stderr.
    result["swap_stdout_tail"] = swap.stdout[-300:] if swap.stdout else ""
    result["swap_stderr_tail"] = swap.stderr[-300:] if swap.stderr else ""
    if swap.returncode != 0:
        result["error"] = (
            f"swap exit {swap.returncode} — see swap_stderr_tail / swap_stdout_tail"
        )
        result["elapsed_seconds"] = round(time.time() - started, 2)
        _emit(result, 1)
        return
    result["swap_ok"] = True

    # Step 2: invoke the QA profile in non-interactive, quiet, single-query mode.
    try:
        proc = subprocess.run(
            [
                "hermes",
                "-p",
                QA_PROFILE,
                "chat",
                "-q",
                PROMPT,
                "-Q",
                "--max-turns",
                MAX_TURNS,
            ],
            capture_output=True,
            text=True,
            timeout=INVOKE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        result["timed_out"] = "invoke"
        result["error"] = f"hermes invoke timed out after {INVOKE_TIMEOUT_SECONDS}s"
        result["elapsed_seconds"] = round(time.time() - started, 2)
        _emit(result, 2)
        return
    except OSError as e:
        result["error"] = f"hermes invoke failed: {e}"
        result["elapsed_seconds"] = round(time.time() - started, 2)
        _emit(result, 2)
        return

    result["invoke_exit"] = proc.returncode
    if proc.returncode != 0:
        result["error"] = (
            f"hermes invoke exit {proc.returncode}: {proc.stderr[-300:]}"
        )
        result["elapsed_seconds"] = round(time.time() - started, 2)
        _emit(result, 2)
        return

    response = proc.stdout.strip()
    response_lower = response.lower()
    result["invoke_ok"] = True
    result["response_excerpt"] = response[:400]
    result["response_says_gemma"] = EXPECTED_TOKEN in response_lower
    result["rival_tokens_detected"] = [
        t for t in RIVAL_TOKENS if t in response_lower
    ]
    result["elapsed_seconds"] = round(time.time() - started, 2)

    if not result["response_says_gemma"]:
        result["error"] = (
            f"response did not contain '{EXPECTED_TOKEN}'; "
            f"rival_tokens_detected={result['rival_tokens_detected']}"
        )
        _emit(result, 3)
        return

    _emit(result, 0)


if __name__ == "__main__":
    main()
