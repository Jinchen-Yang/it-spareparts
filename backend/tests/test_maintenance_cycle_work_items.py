"""D-0058：检测/维修待办不再注册。"""
from app.main import app
from app.services.workflow.registry import registered


def test_cycle_station_subjects_are_not_registered():
    assert app is not None
    specs = registered()
    assert "cycle_test" not in specs
    assert "cycle_repair" not in specs
