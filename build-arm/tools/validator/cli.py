"""CLI — command-line interface for the validator tool.

Usage:
    python -m tools.validator <track_path> [--all] [--artifact NAME]

Arguments:
    track_path  Path to a job's state/tracks/<job-id>/ directory.

Options:
    --all              Validate all 11 artifacts that exist in the track directory.
    --artifact NAME    Validate a single artifact by name (e.g., 'research-input.json').
                       Can be repeated for multiple specific artifacts.

If neither --all nor --artifact is given, defaults to --all.
"""

import argparse
import os
import sys

from . import schema_loader, validator, reporter


def parse_args(argv=None):
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="validator",
        description="Validate auto-think-build contract-chain artifacts against JSON schemas.",
    )
    parser.add_argument(
        "track_path",
        help="Path to a job's state/tracks/<job-id>/ directory.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Validate all artifacts that exist in the track directory.",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="Validate a specific artifact by filename (e.g., 'research-input.json'). "
        "Can be repeated for multiple artifacts.",
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        default=False,
        help="Completeness gate: treat any artifact in the EXPECTED set that is MISSING "
        "from the track as a FAILURE (not just 'validate the ones present'). Without "
        "this, a 4-of-11 chain reports PASS. Use at track close / inside gates, NOT on "
        "the continuous sweep (in-progress tracks are legitimately incomplete).",
    )
    parser.add_argument(
        "--expected",
        default=None,
        help="Comma-separated expected artifact names for --require-complete "
        "(default: all contract-chain artifacts).",
    )

    return parser.parse_args(argv)


def resolve_artifacts(track_path, artifact_names):
    """Find artifact files in the track directory for the given names.

    Args:
        track_path: Path to the track directory.
        artifact_names: List of artifact names (e.g., ['research-input', 'main-review']).

    Returns:
        List of (artifact_name, full_path) tuples for files that exist.
    """
    found = []
    for name in artifact_names:
        # Accept both 'research-input' and 'research-input.json' forms
        if name.endswith(".json"):
            filename = name
        else:
            filename = f"{name}.json"

        full_path = os.path.join(track_path, filename)
        if os.path.isfile(full_path):
            found.append((name.replace(".json", ""), full_path))

    return found


def run(track_path, artifact_names=None, require_complete=False, expected=None):
    """Run validation and print the report.

    Args:
        track_path: Path to the track directory.
        artifact_names: List of artifact names to validate (None = all).
        require_complete: If True, any artifact in the expected set that is MISSING
            from the track is a FAILURE (not just "validate what exists"). This is the
            completeness gate — without it the validator reports PASS on a partial
            chain (e.g. 4 of 11 artifacts present).
        expected: Expected artifact names for the completeness check
            (default: all contract-chain artifacts).

    Returns:
        Exit code (0 = all present pass AND nothing expected is missing; 1 otherwise).
    """
    if not os.path.isdir(track_path):
        print(f"ERROR: Track directory does not exist: {track_path}", file=sys.stderr)
        return 1

    # Determine which artifacts to validate
    if artifact_names:
        targets = artifact_names
    else:
        # Default: validate all known artifacts that exist in the directory
        targets = schema_loader.ALL_ARTIFACTS

    # Find existing artifact files
    found = resolve_artifacts(track_path, targets)

    # Completeness check (opt-in): which expected artifacts are missing entirely?
    completeness = None
    missing = []
    if require_complete:
        expected_set = list(expected) if expected else list(schema_loader.ALL_ARTIFACTS)
        present_names = {name for name, _ in found}
        missing = [name for name in expected_set if name not in present_names]
        completeness = {
            "expected": len(expected_set),
            "present": len(expected_set) - len(missing),
            "missing": missing,
        }

    if not found and not require_complete:
        print(
            f"No artifacts found in {track_path} matching requested names.",
            file=sys.stderr,
        )
        return 1

    # Validate each artifact that exists
    results = []
    for artifact_name, artifact_path in found:
        schema = schema_loader.load_schema(artifact_name)

        if schema is None:
            results.append(
                {
                    "artifact": artifact_name,
                    "path": artifact_path,
                    "pass_": False,
                    "errors": [f"No schema found for artifact '{artifact_name}'"],
                }
            )
            continue

        result = validator.validate_artifact(artifact_path, schema)
        result["artifact"] = artifact_name
        results.append(result)

    # Print report
    report_text = reporter.format_report(results, track_path, completeness=completeness)
    print(report_text)

    # Also output JSON report to stderr for machine consumption
    json_report = reporter.format_report_json(
        results, track_path, completeness=completeness
    )
    print(json_report, file=sys.stderr)

    exit_code = reporter.overall_exit_code(results)
    if missing:
        exit_code = 1
    return exit_code


def main(argv=None):
    """Entry point for the CLI."""
    args = parse_args(argv)

    if args.all or not args.artifact:
        artifact_names = None  # validate all
    else:
        artifact_names = args.artifact

    expected = (
        [s.strip() for s in args.expected.split(",") if s.strip()]
        if args.expected
        else None
    )
    exit_code = run(
        args.track_path,
        artifact_names,
        require_complete=args.require_complete,
        expected=expected,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
