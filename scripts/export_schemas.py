"""Export the public, versioned JSON schemas; run from the repository root."""

import json
from pathlib import Path

from worldforge.models import Run, Scenario, WorldDefinition

directory = Path("schemas")
directory.mkdir(exist_ok=True)
for name, model in [("world", WorldDefinition), ("scenario", Scenario), ("run", Run)]:
    schema = model.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://worldforge.local/schemas/{name}-1.0.json"
    (directory / f"{name}-1.0.json").write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
