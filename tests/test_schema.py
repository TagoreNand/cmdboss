"""Unit tests for the declarative schema DSL and compiler (the RCE replacement)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cmdboss.schema import (
    CITypeDefinition,
    FieldSpec,
    compile_from_definition,
    json_schema_for,
)


def _server_def() -> CITypeDefinition:
    return CITypeDefinition(
        name="server",
        fields={
            "hostname": FieldSpec(type="string", required=True, min_length=1),
            "environment": FieldSpec(
                type="string", required=True, enum=["production", "staging"]
            ),
            "rack_unit": FieldSpec(type="integer", minimum=1, maximum=48),
            "tags": FieldSpec(type="array", items="string"),
        },
    )


def test_compiles_and_validates_payload():
    model = compile_from_definition(_server_def())
    obj = model(hostname="h1", environment="production", rack_unit=12, tags=["a", "b"])
    dumped = obj.model_dump(mode="json")
    assert dumped["hostname"] == "h1"
    assert dumped["rack_unit"] == 12


def test_required_field_missing_is_rejected():
    model = compile_from_definition(_server_def())
    with pytest.raises(ValidationError):
        model(environment="production")  # missing hostname


def test_extra_field_is_forbidden():
    model = compile_from_definition(_server_def())
    with pytest.raises(ValidationError):
        model(hostname="h", environment="production", rogue="x")


def test_enum_value_enforced():
    model = compile_from_definition(_server_def())
    with pytest.raises(ValidationError):
        model(hostname="h", environment="qa")


def test_numeric_bounds_enforced():
    model = compile_from_definition(_server_def())
    with pytest.raises(ValidationError):
        model(hostname="h", environment="staging", rack_unit=999)


def test_array_requires_items():
    with pytest.raises(ValidationError):
        FieldSpec(type="array")  # missing items


def test_enum_only_on_scalar_types():
    with pytest.raises(ValidationError):
        FieldSpec(type="boolean", enum=[True, False])


def test_reference_requires_ref_type():
    with pytest.raises(ValidationError):
        FieldSpec(type="reference")


def test_reserved_and_keyword_field_names_rejected():
    with pytest.raises(ValidationError):
        CITypeDefinition(name="x", fields={"_meta": FieldSpec(type="string")})
    with pytest.raises(ValidationError):
        CITypeDefinition(name="x", fields={"class": FieldSpec(type="string")})


def test_type_name_must_be_safe_token():
    with pytest.raises(ValidationError):
        CITypeDefinition(name="1bad-name!", fields={"a": FieldSpec(type="string")})


def test_index_references_known_fields():
    with pytest.raises(ValidationError):
        CITypeDefinition(
            name="x",
            fields={"a": FieldSpec(type="string")},
            indexes=[{"fields": ["nonexistent"]}],
        )


def test_json_schema_emitted():
    schema = json_schema_for(_server_def())
    assert schema["type"] == "object"
    assert "hostname" in schema["properties"]
    # extra=forbid surfaces as additionalProperties False in JSON Schema
    assert schema.get("additionalProperties") is False
