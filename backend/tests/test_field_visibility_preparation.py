"""批量导出复用请求内脱敏策略，不跨用户或请求缓存授权结果。"""

from copy import deepcopy

import pytest

from app import config, permissions, security


def _payload():
    return {
        "end_customer": "客户甲",
        "items": [{"unit_cost": 12, "cost_amount": 24, "profit": 6, "pn": "PN-1"}],
        "summary": {"reference_side": "purchase", "cost_source": "direct"},
    }


@pytest.mark.parametrize("role", ["admin", "boss", "sales", "purchaser", "readonly", "unknown"])
@pytest.mark.parametrize("perms", [None, {}, {"data_profit": True, "data_purchase_cost": False}])
def test_prepared_visibility_matches_existing_recursive_policy(role, perms, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    ctx = security.UserContext(user_id="viewer", role=role, permissions=perms)
    payload = _payload()
    original = deepcopy(payload)
    expected = security.apply_field_visibility(payload, ctx)

    apply = security.prepare_field_visibility(ctx)
    assert apply(payload) == expected
    assert apply([payload, {"items": [payload]}]) == security.apply_field_visibility(
        [payload, {"items": [payload]}], ctx,
    )
    assert payload == original


def test_prepared_visibility_normalizes_once_and_never_reuses_another_request(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    original_hidden_fields = security._hidden_fields
    calls = []

    def hidden_fields(ctx):
        calls.append(ctx.user_id)
        return original_hidden_fields(ctx)

    monkeypatch.setattr(security, "_hidden_fields", hidden_fields)
    admin = security.UserContext(user_id="admin", role="admin")
    hidden = security.UserContext(user_id="hidden", role="admin", permissions={})
    admin_apply = security.prepare_field_visibility(admin)
    hidden_apply = security.prepare_field_visibility(hidden)
    for _ in range(10):
        assert admin_apply({"unit_cost": 12}) == {"unit_cost": 12}
        assert hidden_apply({"unit_cost": 12}) == {"unit_cost": config.MASK_VALUE}
    assert calls == ["admin", "hidden"]


def test_next_request_prepares_current_permissions_without_global_cache(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    perms = permissions.effective("admin", None)
    ctx = security.UserContext(user_id="same-account", role="admin", permissions=perms)
    first_request = security.prepare_field_visibility(ctx)
    perms["data_purchase_cost"] = False
    next_request = security.prepare_field_visibility(ctx)

    assert first_request({"unit_cost": 12}) == {"unit_cost": 12}
    assert next_request({"unit_cost": 12}) == {"unit_cost": config.MASK_VALUE}


def test_prepared_visibility_keeps_disabled_rbac_passthrough(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_RBAC", False)
    monkeypatch.setattr(security, "_hidden_fields", lambda ctx: pytest.fail("RBAC 已关闭"))
    apply = security.prepare_field_visibility(security.UserContext(user_id="viewer", role="readonly"))
    payload = _payload()
    assert apply(payload) is payload
