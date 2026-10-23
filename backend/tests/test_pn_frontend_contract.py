"""The closed UI vocabulary must match the canonical permission registry."""
import json
from pathlib import Path

from app.authz.pn_resources import CATALOG_ACTIONS, PN_RESOURCES


def test_frontend_catalog_permission_vocabulary_is_current():
    registry = json.loads((Path(__file__).parents[2] / "frontend/src/authz/pnResources.json").read_text())
    assert registry == {key: {"actions": list(value["actions"]), "fields": list(value["fields"])}
                        for key, value in PN_RESOURCES.items()}
    actions = json.loads((Path(__file__).parents[2] / "frontend/src/authz/pnActions.json").read_text())
    assert actions == {key: list(value) for key, value in CATALOG_ACTIONS.items()}
