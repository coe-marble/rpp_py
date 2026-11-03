"""Parameter defaults, component overrides, and parameter-module loading."""

import sys
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from rpp_common.parameter_description import ParameterDescription
from rpp_py.parameter_handler import ParameterHandler, Parameters


@pytest.fixture(params=["metadata", "descriptions"])
def describe(request):
    def metadata(value):
        if isinstance(value, dict):
            return {"type": "object", "fields": {
                name: {"name": name, **metadata(item)} for name, item in value.items()
            }}
        if isinstance(value, list):
            return {"type": "array", "default_value": [metadata(item) for item in value]}
        scalar_types = {bool: "bool", int: "int", float: "float", str: "string"}
        return {"type": scalar_types[type(value)], "default_value": value}

    def build(defaults):
        if request.param == "metadata":
            return {name: metadata(value) for name, value in defaults.items()}
        return [ParameterDescription(name, value) for name, value in defaults.items()]

    return build


def test_resolution_uses_defaults_for_missing_overrides(describe):
    resolved = ParameterHandler.resolve_params(
        describe({"gain": 2.5, "enabled": True}), SimpleNamespace())

    assert resolved.get("gain") == 2.5
    assert resolved.get("enabled") is True


@pytest.mark.parametrize("override", [0, False, "", None, [], {}])
def test_explicit_overrides_take_precedence_even_when_falsy(describe, override):
    resolved = ParameterHandler.resolve_params(
        describe({"value": 10}), SimpleNamespace(value=override))

    assert resolved.get("value") == override
    assert type(resolved.get("value")) is type(override)


def test_resolution_ignores_undeclared_attributes(describe):
    resolved = ParameterHandler.resolve_params(
        describe({"gain": 2.5}), SimpleNamespace(gain=4.0, unrelated=99))

    assert resolved.get("gain") == 4.0
    assert resolved.get("unrelated", "missing") == "missing"


def test_empty_descriptions_produce_empty_parameters(describe):
    resolved = ParameterHandler.resolve_params(describe({}), SimpleNamespace(extra=1))

    assert resolved.get("extra", "missing") == "missing"


def test_nested_object_and_array_defaults_are_resolved():
    description = {"controller": {
        "type": "object", "fields": {
            "enabled": {"name": "enabled", "type": "bool", "default_value": False},
            "gains": {"name": "gains", "type": "array", "default_value": [
                {"type": "float", "default_value": 1.5},
                {"type": "object", "fields": {
                    "limit": {"name": "limit", "type": "int", "default_value": 10},
                }},
            ]},
        },
    }}

    resolved = ParameterHandler.resolve_params(description, SimpleNamespace())

    assert resolved.get("controller") == {"enabled": False, "gains": [1.5, {"limit": 10}]}


def test_nested_override_replaces_the_whole_default_value():
    description = {"controller": {"type": "object", "fields": {
        "gain": {"name": "gain", "type": "float", "default_value": 2.0},
    }}}

    resolved = ParameterHandler.resolve_params(
        description, SimpleNamespace(controller={"gain": 7.0, "limit": 5}))

    assert resolved.get("controller") == {"gain": 7.0, "limit": 5}


@pytest.mark.parametrize("info, expected", [
    ({"type": "array"}, []),
    ({"type": "object", "fields": {}}, {}),
    ({"type": "string", "default_value": None}, None),
])
def test_empty_and_none_defaults(info, expected):
    assert ParameterHandler.resolve_default_value(info) == expected


def test_missing_scalar_default_reports_invalid_description():
    with pytest.raises(RuntimeError, match="does not contain a default value"):
        ParameterHandler.resolve_params({"gain": {"type": "float"}}, SimpleNamespace())


def test_mutating_resolved_defaults_does_not_affect_other_components(describe):
    description = describe({"settings": {"gains": [1.0]}})
    first = ParameterHandler.resolve_params(description, SimpleNamespace())
    second = ParameterHandler.resolve_params(description, SimpleNamespace())

    first.get("settings")["gains"].append(2.0)

    assert second.get("settings") == {"gains": [1.0]}
    third = ParameterHandler.resolve_params(description, SimpleNamespace())
    assert third.get("settings") == {"gains": [1.0]}


def test_get_distinguishes_missing_values_from_explicit_none():
    parameters = Parameters({"optional": None})

    assert parameters.get("missing", 42) == 42
    assert parameters.get("optional", 42) is None


@dataclass
class ControllerSettings:
    gain: float = 1.0
    enabled: bool = True


def test_get_as_populates_fields_and_keeps_unspecified_class_defaults():
    settings = Parameters({"controller": {"gain": 3.0}}).get_as(
        "controller", ControllerSettings)

    assert settings == ControllerSettings(gain=3.0, enabled=True)


def test_get_as_uses_fallback_for_missing_parameter():
    assert Parameters().get_as(
        "missing", ControllerSettings, {"enabled": False}) == ControllerSettings(enabled=False)


@pytest.mark.parametrize("value", [None, 3, "text", []])
def test_get_as_rejects_non_dictionary_values(value):
    with pytest.raises(TypeError, match="not a dictionary"):
        Parameters({"controller": value}).get_as("controller", ControllerSettings)


def test_get_as_requires_a_type():
    with pytest.raises(TypeError, match="Expected type must be a type"):
        Parameters({"controller": {}}).get_as("controller", ControllerSettings())


def test_get_as_rejects_unknown_fields():
    with pytest.raises(AttributeError, match="no attribute 'unknown'"):
        Parameters({"controller": {"unknown": 3}}).get_as("controller", ControllerSettings)


@pytest.fixture
def parameter_file(tmp_path, monkeypatch):
    # Preserve any pre-existing module when pytest restores the monkeypatch.
    monkeypatch.delitem(sys.modules, "parameters__", raising=False)
    folder = tmp_path / "component"
    (folder / "params").mkdir(parents=True)
    return folder / "params" / "parameters.py"


def test_load_parameters_and_resolve_component_overrides(parameter_file):
    parameter_file.write_text(
        "class ComponentParameters:\n    gain = 4.0\n    enabled = False\n", encoding="utf-8")

    with ParameterHandler(parameter_file.parent.parent) as handler:
        loaded = handler.load_parameters_from_python_module()
        resolved = handler.resolve_params([
            ParameterDescription("gain", 1.0),
            ParameterDescription("enabled", True),
            ParameterDescription("limit", 10),
        ], loaded)

    assert (resolved.get("gain"), resolved.get("enabled"), resolved.get("limit")) == (
        4.0, False, 10)
    assert "parameters__" not in sys.modules


@pytest.mark.parametrize("source, exception, message", [
    (None, RuntimeError, "Parameters module not found"),
    ("gain = 1\n", RuntimeError, "does not contain a 'ComponentParameters' class"),
    ("class ComponentParameters(:\n", SyntaxError, "invalid syntax"),
    ("raise ValueError('invalid component parameters')\n", ValueError,
     "invalid component parameters"),
])
def test_parameter_module_failure_cleans_up_import_state(
        parameter_file, source, exception, message):
    if source is not None:
        parameter_file.write_text(source, encoding="utf-8")

    with pytest.raises(exception, match=message):
        with ParameterHandler(parameter_file.parent.parent) as handler:
            handler.load_parameters_from_python_module()

    assert "parameters__" not in sys.modules
