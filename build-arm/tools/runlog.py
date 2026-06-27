"""Per-run metrics logger — appends JSON lines to an events file."""

from datetime import datetime, timezone
import json
import os


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp string to a timezone-aware datetime."""
    # Handle 'Z' suffix (Python < 3.11 doesn't always accept it)
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts)


def log_run(
    events_path: str,
    *,
    role: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    started_at: str,
    ended_at: str,
    swap_occurred: bool = False,
) -> dict:
    """Append one metrics record to *events_path* and return it.

    Parameters
    ----------
    events_path : str
        Path to the JSON-lines file. Created / appended if it does not exist.
    role : str
        The model role (e.g. "user", "assistant").
    model : str
        Model identifier string.
    prompt_tokens : int
        Number of tokens in the prompt.
    completion_tokens : int
        Number of tokens in the completion.
    started_at : str
        ISO-8601 timestamp when the run began.
    ended_at : str
        ISO-8601 timestamp when the run finished.
    swap_occurred : bool, optional
        Whether a model swap occurred (default False).

    Returns
    -------
    dict
        The record that was appended.

    Raises
    ------
    ValueError
        If *ended_at* is earlier than *started_at*.
    """

    start_dt = _parse_iso(started_at)
    end_dt = _parse_iso(ended_at)

    if end_dt < start_dt:
        raise ValueError(
            f"ended_at ({ended_at}) is earlier than started_at ({started_at})"
        )

    # wall_ms: integer milliseconds between the two timestamps
    delta = (end_dt - start_dt).total_seconds()
    wall_ms = int(delta * 1000)

    record = {
        "type": "run-metrics",
        "role": role,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "wall_ms": wall_ms,
        "swap_occurred": swap_occurred,
        "started_at": started_at,
        "ended_at": ended_at,
    }

    # Append one JSON line; never overwrite existing content.
    with open(events_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    return record
