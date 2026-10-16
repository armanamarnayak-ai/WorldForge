"""Export the public, versioned JSON schemas; run from the repository root."""

import json
from pathlib import Path

from worldforge.models import (
    FailureCertificate,
    ResourceReport,
    Run,
    Scenario,
    SearchSpec,
    WorldDefinition,
)

directory = Path("schemas")
directory.mkdir(exist_ok=True)
# The released 1.0 schemas are historical contracts and must not be overwritten.
for name, version, model in [
    ("world", "1.1", WorldDefinition),
    ("scenario", "1.1", Scenario),
    ("run", "1.1", Run),
    ("failure-certificate", "1.0", FailureCertificate),
    ("resource-report", "1.0", ResourceReport),
    ("search", "1.0", SearchSpec),
]:
    schema = model.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://worldforge.local/schemas/{name}-{version}.json"
    (directory / f"{name}-{version}.json").write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
