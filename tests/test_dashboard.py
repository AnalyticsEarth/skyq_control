"""docs/dashboard.yaml only refers to entities the integration creates for a gateway."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import make_entry, setup_entry

pytestmark = pytest.mark.usefixtures("mock_skyq")

DASHBOARD = Path(__file__).parents[1] / "docs" / "dashboard.yaml"
ENTITY_ID = re.compile(r"\b(?:[a-z_]+)\.sky_q_gateway(?:_[a-z0-9_]+)?\b")


async def test_dashboard_entities_exist(hass: HomeAssistant, entity_registry: er.EntityRegistry) -> None:
    config = yaml.safe_load(DASHBOARD.read_text())
    assert config["views"][0]["sections"]
    referenced = set(ENTITY_ID.findall(DASHBOARD.read_text()))
    assert len(referenced) > 50 and "media_player.sky_q_gateway" in referenced

    entry = await setup_entry(hass, make_entry())
    created = {e.entity_id for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id)}
    assert sorted(referenced - created) == []
    assert not any(entity_registry.async_get(e).disabled for e in referenced)
