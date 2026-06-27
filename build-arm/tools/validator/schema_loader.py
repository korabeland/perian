"""Schema loader — discover and load JSON schemas from the /schemas/ directory."""

import json
import os


# Canonical artifact names and their corresponding schema file stems.
ARTIFACT_SCHEMA_MAP = {
    "research-input": "research-input.schema.json",
    "idea-contract": "idea-contract.schema.json",
    "intent-review": "intent-review.schema.json",
    "main-review": "main-review.schema.json",
    "product-plan": "product-plan.schema.json",
    "build-plan": "build-plan.schema.json",
    "verification": "verification.schema.json",
    "qa-verification": "qa-verification.schema.json",
    "verification-delta": "verification-delta.schema.json",
    "trust-report": "trust-report.schema.json",
    "retention-review": "retention-review.schema.json",
}

# Reverse mapping: schema file stem -> artifact name.
SCHEMA_ARTIFACT_MAP = {v: k for k, v in ARTIFACT_SCHEMA_MAP.items()}

# Ordered list of all 11 artifact names (pipeline sequence).
ALL_ARTIFACTS = list(ARTIFACT_SCHEMA_MAP.keys())


def _find_schemas_dir():
    """Locate the /schemas/ directory relative to this package.

    The validator lives at <root>/tools/validator/, so the schemas dir is
    two levels up from this file's parent (i.e. <root>/schemas/).
    """
    package_dir = os.path.dirname(os.path.abspath(__file__))
    # validator/ -> tools/ -> auto-think-build/ (root)
    root = os.path.dirname(os.path.dirname(package_dir))
    return os.path.join(root, "schemas")


def load_schema(artifact_name):
    """Load and return the JSON schema dict for *artifact_name*.

    Returns None if no matching schema file exists.
    """
    schema_file = ARTIFACT_SCHEMA_MAP.get(artifact_name)
    if schema_file is None:
        return None

    schemas_dir = _find_schemas_dir()
    schema_path = os.path.join(schemas_dir, schema_file)

    if not os.path.isfile(schema_path):
        return None

    with open(schema_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def list_available_schemas():
    """Return a sorted list of artifact names that have schema files on disk."""
    schemas_dir = _find_schemas_dir()
    if not os.path.isdir(schemas_dir):
        return []

    available = []
    for filename in sorted(os.listdir(schemas_dir)):
        if filename.endswith(".schema.json"):
            artifact_name = SCHEMA_ARTIFACT_MAP.get(filename)
            if artifact_name:
                available.append(artifact_name)
    return available
