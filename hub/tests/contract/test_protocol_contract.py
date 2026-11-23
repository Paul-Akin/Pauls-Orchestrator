"""Contract tests: hub models <-> protocol/schemas cannot drift.

1. Every fixture in protocol/examples/valid must pass BOTH jsonschema and the
   generated pydantic model; every fixture in invalid/ must fail BOTH.
2. Every schema file must have a generated model and fixtures, and vice versa.
3. The generated model's required fields/properties match the schema's.

CI additionally regenerates the models and fails on any diff.

Known, accepted gap: calendar validity of ``ts`` (e.g. Feb 30) is enforced by
jsonschema's ``format: date-time`` checker only; the generated model checks the
regex. The hub always runs jsonschema first (ProtocolValidator.parse), so
behaviour is correct; this is why the fixtures stop at regex-detectable errors.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from hub.events.validate import SCHEMA_FILES, ProtocolValidator
from hub.paths import repo_root, schemas_dir

EXAMPLES = repo_root() / "protocol" / "examples"
MODEL_BY_SCHEMA = {name.removesuffix(".json"): model for model, name in SCHEMA_FILES.items()}
VALIDATOR = ProtocolValidator()


def _fixtures(kind: str) -> list[tuple[str, Path]]:
    return [(p.parent.name, p) for p in sorted((EXAMPLES / kind).glob("*/*.json"))]


def test_every_schema_has_a_model_and_fixtures() -> None:
    on_disk = {p.name.removesuffix(".json") for p in schemas_dir().glob("*.json")}
    assert on_disk == set(MODEL_BY_SCHEMA), "schema files and SCHEMA_FILES differ"
    for name in on_disk:
        assert any((EXAMPLES / "valid" / name).glob("*.json")), f"no valid fixtures for {name}"
        assert any((EXAMPLES / "invalid" / name).glob("*.json")), f"no invalid fixtures for {name}"


@pytest.mark.parametrize(
    ("schema", "path"), _fixtures("valid"), ids=lambda x: str(getattr(x, "name", x))
)
def test_valid_fixture_accepted_by_both(schema: str, path: Path) -> None:
    model = MODEL_BY_SCHEMA[schema]
    data = json.loads(path.read_text())
    assert VALIDATOR.schema_errors(model, data) == []
    model.model_validate(data)


@pytest.mark.parametrize(
    ("schema", "path"), _fixtures("invalid"), ids=lambda x: str(getattr(x, "name", x))
)
def test_invalid_fixture_rejected_by_both(schema: str, path: Path) -> None:
    model = MODEL_BY_SCHEMA[schema]
    data = json.loads(path.read_text())
    assert VALIDATOR.schema_errors(model, data), "jsonschema accepted an invalid fixture"
    with pytest.raises(ValidationError):
        model.model_validate(data)


@pytest.mark.parametrize("model", list(SCHEMA_FILES), ids=lambda m: m.__name__)
def test_model_shape_matches_schema(model: type[BaseModel]) -> None:
    schema = json.loads((schemas_dir() / SCHEMA_FILES[model]).read_text())
    assert set(model.model_fields) == set(schema["properties"])
    required = {n for n, f in model.model_fields.items() if f.is_required()}
    assert required == set(schema["required"])
    assert model.model_config.get("extra") == "forbid"
    assert schema["additionalProperties"] is False
