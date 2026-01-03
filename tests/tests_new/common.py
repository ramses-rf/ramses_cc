"""Tests for the ramses_cc tests."""

from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import patch

from homeassistant.util.json import JsonObjectType
from homeassistant.util.yaml import parse_yaml
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    load_fixture,
    load_json_object_fixture,
)

from custom_components.ramses_cc import DOMAIN


# NOTE: Doesn't work with YAML?
def get_fixture_path(filename: str, integration: str | None = None) -> pathlib.Path:
    """Get path of fixture."""
    return pathlib.Path(__file__).parent.joinpath("fixtures", filename)


def load_yaml_object_fixture(
    filename: str, integration: str | None = None
) -> dict[str, Any]:
    """Load a YAML dict from a fixture."""
    return parse_yaml(load_fixture(filename, integration))


@patch(
    "pytest_homeassistant_custom_component.common.get_fixture_path", get_fixture_path
)
def configuration_fixture(instance: str) -> dict[str, Any]:
    """Return the configuration for an instance of the integration."""
    try:
        return load_yaml_object_fixture(f"{instance}/configuration.yaml", DOMAIN)
    except FileNotFoundError:
        return load_yaml_object_fixture("default/configuration.yaml", DOMAIN)


@patch(
    "pytest_homeassistant_custom_component.common.get_fixture_path", get_fixture_path
)
def storage_fixture(instance: str) -> JsonObjectType:
    """Return the storage for an instance of the integration."""
    try:
        return load_json_object_fixture(f"{instance}/storage.json", DOMAIN)
    except FileNotFoundError:
        return load_json_object_fixture("minimal/storage.json", DOMAIN)
