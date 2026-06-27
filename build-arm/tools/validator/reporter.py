"""Reporter — format validation results into structured pass/fail reports."""

import json


def format_report(results, track_path, completeness=None):
    """Format a list of validation results into a human-readable report.

    Args:
        results: List of result dicts from validator.validate_artifact.
        track_path: Path to the track directory being validated.
        completeness: Optional {expected, present, missing:[...]} from a
            --require-complete run; rendered + folded into the overall verdict.

    Returns:
        A string containing the formatted report.
    """
    lines = []
    lines.append(f"Validator Report — track: {track_path}")
    lines.append("=" * 60)

    incomplete = bool(completeness and completeness.get("missing"))

    if not results:
        lines.append("No artifacts found to validate.")
        if incomplete:
            lines.append("  MISSING: " + ", ".join(completeness["missing"]))
            lines.append(
                f"Overall: INCOMPLETE — {len(completeness['missing'])} expected artifact(s) missing"
            )
        return "\n".join(lines)

    passed = sum(1 for r in results if r.get("pass_"))
    failed = len(results) - passed

    for result in results:
        artifact = result.get("artifact", "unknown")
        status = "PASS" if result.get("pass_") else "FAIL"
        lines.append(f"\n  [{status}] {artifact}")

        if result.get("errors"):
            for error in result["errors"]:
                lines.append(f"    - {error}")

    lines.append("\n" + "=" * 60)
    lines.append(
        f"Summary: {passed} passed, {failed} failed out of {len(results)} artifacts"
    )

    if completeness is not None:
        lines.append(
            f"Completeness: {completeness['present']}/{completeness['expected']} expected artifacts present"
        )
        if completeness["missing"]:
            lines.append("  MISSING: " + ", ".join(completeness["missing"]))

    if failed == 0 and not incomplete:
        lines.append("Overall: ALL PASSED")
    elif failed == 0 and incomplete:
        lines.append(
            f"Overall: INCOMPLETE — {len(completeness['missing'])} expected artifact(s) missing"
        )
    else:
        suffix = f" + {len(completeness['missing'])} MISSING" if incomplete else ""
        lines.append(f"Overall: {failed} ARTIFACT(S) FAILED{suffix}")

    return "\n".join(lines)


def format_report_json(results, track_path, completeness=None):
    """Format results as a JSON report (machine-readable).

    Args:
        results: List of result dicts from validator.validate_artifact.
        track_path: Path to the track directory being validated.
        completeness: Optional {expected, present, missing:[...]} from a
            --require-complete run; included in the JSON and folded into overall_pass.

    Returns:
        A JSON string containing the structured report.
    """
    passed = sum(1 for r in results if r.get("pass_"))
    failed = len(results) - passed
    incomplete = bool(completeness and completeness.get("missing"))

    report = {
        "track": track_path,
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "overall_pass": failed == 0 and not incomplete,
        "artifacts": [],
    }
    if completeness is not None:
        report["completeness"] = completeness

    for result in results:
        artifact_entry = {
            "artifact": result.get("artifact", "unknown"),
            "path": result.get("path", ""),
            "pass": result.get("pass_", False),
        }
        if result.get("errors"):
            artifact_entry["errors"] = result["errors"]
        report["artifacts"].append(artifact_entry)

    return json.dumps(report, indent=2)


def overall_exit_code(results):
    """Return the appropriate exit code for a set of validation results.

    Returns 0 if all artifacts passed, 1 if any failed.
    """
    if not results:
        return 0  # No artifacts to validate is not a failure

    if all(r.get("pass_") for r in results):
        return 0

    return 1
