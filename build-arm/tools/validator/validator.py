"""Validator — core validation logic for contract-chain artifacts."""

import json

from jsonschema import Draft202012Validator


def validate_artifact(artifact_path, schema):
    """Validate a JSON artifact file against a JSON schema.

    Args:
        artifact_path: Path to the JSON artifact file.
        schema: Parsed JSON schema dict (from schema_loader.load_schema).

    Returns:
        A dict with keys:
            - artifact (str): basename of the file
            - path (str): full path to the artifact
            - pass_ (bool): True if validation passed
            - errors (list[str]): human-readable error messages (empty if pass)
    """
    result = {
        "artifact": None,
        "path": artifact_path,
        "pass_": False,
        "errors": [],
    }

    # Load artifact JSON
    try:
        with open(artifact_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, FileNotFoundError, PermissionError) as exc:
        result["errors"].append(f"Failed to load artifact: {exc}")
        return result

    # Validate against schema
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))

    if not errors:
        result["pass_"] = True
        return result

    for error in errors:
        # Build a human-readable path (e.g. "sources[0].name" or top-level "job_id")
        if error.absolute_path:
            path_str = ".".join(str(p) for p in error.absolute_path)
            field_ref = path_str
        else:
            field_ref = "(root)"

        msg = f"{field_ref}: {error.message}"
        result["errors"].append(msg)

    return result
