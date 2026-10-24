"""Read-only S3 design checks; no database, HTTP, application writes or runtime proof.

Run with Python 3 from any directory. The legacy permission module is pure Python;
this script loads only that file to verify real persisted-user effective semantics.
It does not run migrations or claim source-to-runtime semantic equivalence.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import copy
import argparse
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def read_json(name):
    return json.loads((HERE / name).read_text())


def evaluate(node, values):
    assert isinstance(node, dict) and len(node) == 1, node
    kind, body = next(iter(node.items()))
    if kind == "predicate":
        assert body in values, body
        return values[body]
    if kind == "constant":
        assert type(body) is bool
        return body
    if kind == "not":
        return not evaluate(body, values)
    assert kind in {"all", "any"} and isinstance(body, list) and body
    results = [evaluate(child, values) for child in body]
    return all(results) if kind == "all" else any(results)


def select_scope(node, values):
    if isinstance(node, str):
        return node
    assert set(node) == {"cases", "else"}
    for case in node["cases"]:
        if evaluate(case["if"], values):
            return select_scope(case["scope"], values)
    return select_scope(node["else"], values)


def seed(resource, values):
    actions, fields = set(), set()
    for rule in resource["seed"]["rules"]:
        if evaluate(rule["if"], values):
            actions.update(rule["actions"])
            fields.update(rule["field_permissions"])
    return actions, select_scope(resource["seed"]["scope"], values), fields


def source_bytes(path, source_ref=None):
    if source_ref is None:
        return (ROOT / path).read_bytes()
    # Frozen hashes are unchanged. This verifies the actual historical blob,
    # rather than blessing the implementation by replacing its old evidence.
    return subprocess.check_output(["git", "show", f"{source_ref}:{path}"], cwd=ROOT)


def main(*, spec_override=None, emit=True, source_ref=None):
    spec = spec_override if spec_override is not None else read_json("s3-execution-spec.json")
    base = read_json("s3-resource-seed-gaps.json")
    resources = {r["resource_id"]: r for r in spec["resources"]}
    assert len(resources) == len(spec["resources"]) == spec["counts"]["execution_resources"] == 73
    assert set(spec["candidate_to_execution_resources"]) == {r["resource_id"] for r in base["resources"]}
    assert {r for values in spec["candidate_to_execution_resources"].values() for r in values} == set(resources)
    expected_entries = {e["entry_id"] for r in base["resources"] for e in r["entries"]}
    entries = {e["entry_id"]: e for e in spec["entries"]}
    assert len(entries) == len(spec["entries"]) == 228
    assert set(entries) == expected_entries
    predicates = spec["seed_protocol"]["predicates"]
    dummy_values = {key: False for key in predicates}
    action_count = 0
    action_kinds = {"data": 0, "mutation": 0}
    sources = {x["file"]: x["sha256"] for x in spec["supporting_sources"]}
    for e in entries.values():
        assert e["bindings"], e["entry_id"]
        source = e["source"]
        if source["file"] in sources:
            assert sources[source["file"]] == source["sha256"]
        sources[source["file"]] = source["sha256"]
    for path, expected in sources.items():
        actual = hashlib.sha256(source_bytes(path, source_ref)).hexdigest()
        assert actual == expected, ("source drift", path, expected, actual)
    contexts = spec["context_protocol"]
    object_types = set(contexts["closed_object_types"])
    assert len(object_types) == len(contexts["closed_object_types"])
    assert set(contexts["closed_internal_context_kinds"]) == {"project", "collection"} | object_types
    assert contexts["schema"]["context"]["kind"] == "project | collection | object"
    collection_refs = {c["id"] for c in contexts["closed_collection_refs"]}
    for r in resources.values():
        ids = {a["id"] for a in r["actions"]}
        fields = set(r["field_permissions"])
        assert len(ids) == len(r["actions"])
        action_count += len(ids)
        for a in r["actions"]:
            assert a["kind"] in action_kinds and not a["id"].startswith("show_")
            action_kinds[a["kind"]] += 1
            assert set(a["requires_fields"]) <= fields
            assert set(a["requires_actions"]) <= ids
            assert set(a["requires_any_actions"]) <= ids
        for source, target in r["field_dependencies"]:
            assert {source, target} <= fields
        scopes = {s["id"] for s in r["scopes"]}
        assert all(s["requires_context"] is True for s in r["scopes"])
        assert r["denial_scope"] in scopes
        assert select_scope(r["seed"]["scope"], dummy_values) in scopes
        for rule in r["seed"]["rules"]:
            evaluate(rule["if"], dummy_values)
            assert set(rule["actions"]) <= ids
            assert set(rule["field_permissions"]) <= fields
        assert set(r["allowed_context_kinds"]) <= set(contexts["closed_internal_context_kinds"])
        assert set(r["allowed_collection_refs"]) <= collection_refs
        family = spec["query_families"][r["query_family"]]
        assert r["query_attributes"] == family["query_attributes"]
        assert r["context_only_without_bindings"] == (r["query_family"] == "context_only")
        assert r["query_attributes"] or r["context_only_without_bindings"]

    pair_count = 0
    def pair(value):
        nonlocal pair_count
        assert isinstance(value, list) and len(value) == 2, value
        resource, action = value
        assert resource in resources, value
        assert action in {a["id"] for a in resources[resource]["actions"]}, value
        pair_count += 1

    def walk(value):
        if isinstance(value, dict):
            if "resource_id" in value and "action" in value:
                pair([value["resource_id"], value["action"]])
            for key, child in value.items():
                if key == "require" and isinstance(child, list):
                    pair(child)
                elif key in {"also_require", "select_one_complete_contract"}:
                    for item in child:
                        pair(item)
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    for entry in entries.values():
        walk(entry["bindings"])
    walk(spec["workspace"])
    walk(spec["master_operations"])
    assert "cost_gaps" not in {b["id"] for b in spec["workspace"]["blocks"]}
    assert "read_workspace" not in {a["id"] for a in resources["maintenance_cost_fill"]["actions"]}

    # Exact source-bound entry contexts, not a permissive union of resource kinds.
    def entry_pairs(entry):
        found = set()
        def visit(value):
            if isinstance(value, dict):
                if "resource_id" in value and "action" in value:
                    found.add((value["resource_id"], value["action"]))
                for key, child in value.items():
                    if key == "require" and isinstance(child, list):
                        found.add(tuple(child))
                    elif key in {"also_require", "select_one_complete_contract"}:
                        found.update(map(tuple, child))
                    else:
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(entry["bindings"])
        return found

    def closed_schema(schema):
        assert isinstance(schema, dict) and "$ref" not in schema
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False, schema
            assert set(schema.get("required", [])) <= set(schema.get("properties", {}))
        for key, value in schema.items():
            if key in {"properties", "$defs"}:
                for child in value.values():
                    closed_schema(child)
            elif key == "items":
                closed_schema(value)
            elif key in {"anyOf", "oneOf", "allOf"}:
                for child in value:
                    closed_schema(child)

    collection_registry = {c["id"]: c for c in contexts["closed_collection_refs"]}
    assert len(collection_registry) == len(contexts["closed_collection_refs"])
    input_registry = {(i["entry_id"], i["purpose"]): i for i in contexts["input_authorization_view_resolvers"]}
    assert len(input_registry) == len(contexts["input_authorization_view_resolvers"])
    collection_usage, input_usage = set(), set()
    bound_actions = set()
    for entry_id, entry in entries.items():
        requirement = entry["context_requirement"]
        slots = requirement.get("all_of", [requirement])
        assert slots and len({s["slot"] for s in slots}) == len(slots)
        pairs = entry_pairs(entry)
        bound_actions.update(pairs)
        assert {(p["resource_id"], p["action"]) for p in requirement["policy_subjects"]} == pairs
        for slot in slots:
            assert slot["kind"] in {"project", "object", "collection", "none"}
            if slot["kind"] == "none":
                assert entry_id == "POST /api/maintenance/projects/stable" and not pairs
                continue
            assert slot.get("resolver"), (entry_id, slot)
            if "selector_schema" in slot:
                closed_schema(slot["selector_schema"])
            if slot["kind"] == "object":
                assert slot["object_type"] in object_types
            if slot["kind"] == "collection":
                assert slot["id"] in collection_registry
                assert slot["purpose"] and "selector_schema" in slot
                assert not {"page", "page_size", "limit", "offset", "focus_page", "focus_page_size"} & set(slot["selector_schema"]["properties"])
                matching = [p for p in collection_registry[slot["id"]]["purposes"]
                            if p["entry_id"] == entry_id and p["purpose"] == slot["purpose"]]
                assert len(matching) == 1 and matching[0]["selector_schema"] == slot["selector_schema"]
                collection_usage.add((slot["id"], slot["purpose"], entry_id))
                if any(next(a for a in resources[r]["actions"] if a["id"] == action)["kind"] == "mutation" for r, action in pairs):
                    assert slot.get("authority") in {"discovery_only", "discovery_only_non_authorizing"}
                    assert any(s.get("object_type") == "operation_input" for s in slots)
            if slot.get("object_type") == "operation_input":
                key = (entry_id, slot["purpose"])
                assert key in input_registry
                item = input_registry[key]
                assert item["selector_schema"] == slot["selector_schema"]
                assert item["binding_expression_NEVER_FLATTEN"] == entry["bindings"]
                assert {tuple(p) for p in item["allowed_resource_actions"]} == pairs
                assert requirement["operation_admission"]["before_target_quantifier"] is True
                assert item["view"] == "authorization" and len(item["no_side_effects"]) >= 3
                input_usage.add(key)
        for resource, action in pairs:
            contracts = resources[resource]["action_context_contract"][action]
            selected = [c for c in contracts if c["entry_id"] == entry_id]
            assert len(selected) == 1 and selected[0]["context_slots"]
            assert {c["slot"] for c in selected[0]["context_slots"]} == {s["slot"] for s in slots if s["kind"] != "none"}
        for subject in requirement["policy_subjects"]:
            if resources[subject["resource_id"]]["query_family"] == "project":
                assert subject["cannot_supply_reduced_booleans"] is True
                assert subject["quantifier"] == "each_object_full_policy_then_ALL"
    assert bound_actions == {(r, a["id"]) for r, item in resources.items() for a in item["actions"]}
    # Reviewed implementation branches (unassigned source / replay / discovery)
    # are exact entry alternatives, never a resource-wide context-kind union.
    for entry_id, entry in entries.items():
        for alternative in entry.get("runtime_topology_alternatives", {}).values():
            for resource, actions in alternative["requirements"].items():
                assert resource in resources
                assert set(actions) <= {a["id"] for a in resources[resource]["actions"]}
            for slot in alternative["slots"].values():
                closed_schema(slot["selector_schema"])
                if slot["kind"] == "object":
                    assert slot["object_type"] in object_types
                if slot["kind"] == "collection":
                    registered = collection_registry[slot["collection_ref"]]
                    matching = [p for p in registered["purposes"]
                                if p["entry_id"] == entry_id and p["purpose"] == slot["purpose"]]
                    assert len(matching) == 1 and matching[0]["selector_schema"] == slot["selector_schema"]
                    collection_usage.add((slot["collection_ref"], slot["purpose"], entry_id))
    assert collection_usage == {(r, p["purpose"], p["entry_id"]) for r, item in collection_registry.items() for p in item["purposes"]}
    assert input_usage == set(input_registry)
    assert contexts["operation_input_contract"]["exact_action_allowlist"] == contexts["input_authorization_view_resolvers"]
    for resolver in contexts["authorization_view_resolvers"]:
        assert f'{resolver["method"]} {resolver["path"]}' in entries
        assert resolver["ref"] == "project" or resolver["ref"] in collection_registry
    warehouse_apply = entries["POST /api/maintenance/warehouse-imports/{import_id}/apply"]["context_requirement"]
    assert warehouse_apply["quantifier"] == "exactly_one_branch_selected_by_actual_import_existence_after_server_input_verification"
    assert {s["object_type"] for s in warehouse_apply["all_of"]} == {"warehouse_import", "operation_input"}
    assert all(s.get("when") for s in warehouse_apply["all_of"])
    assert entries["POST /api/maintenance/ai-fallback/propose"]["bindings"][0]["branches"]
    assert entries["POST /api/maintenance/collection-plan-imports/{batch_id}/apply"]["context_requirement"]["additional_domain_slots"]
    for entry_id, entry in entries.items():
        if "/master-workbook/" in entry_id and entry_id.endswith(("/validate", "/apply")):
            additional = entry["context_requirement"]["additional_domain_slots"]
            assert any(s["object_type"] == "source_order" and s["quantifier"] == "ALL"
                       and s["reject_missing_or_partial"] is True for s in additional)

    # Real legacy effective graphs, including intentionally dirty persisted input.
    module_spec = importlib.util.spec_from_file_location("s3_legacy_permissions", ROOT / "backend/app/permissions.py")
    perm = importlib.util.module_from_spec(module_spec)
    exec(compile(source_bytes("backend/app/permissions.py", source_ref), str(module_spec.origin), "exec"), perm.__dict__)

    def values_for(user):
        raw = perm.effective_for_user(user)
        graph = perm.runtime_safe(raw)
        stored = (perm.effective_from_snapshot(user.template_perms, user.perm_overrides)
                  if user.template_perms is not None else perm.sanitize(user.permissions))
        admin = user.role == "admin"
        full = user.role in {"admin", "boss", "phase1_full_access"}
        values = {
            "P": perm.page_permission_allowed(role=user.role, permission_map=graph, page_key="page_maintenance"),
            "Beta": graph["page_maintenance_beta"], "admin": admin,
            "full_scope": full, "FULL": full,
            "maintenance_scoped": not full and graph["own_maintenance_projects_only"],
            "source_sales_scoped": graph["own_customers_only"],
            "C": graph["data_purchase_cost"], "F": graph["data_profit"], "K": graph["data_customer"],
            "upload_action": graph["action_maintenance_expense_collection_upload"],
            "project_manage_action": graph["action_maintenance_project_manage"],
            "roundtrip_apply_action": graph["action_maintenance_roundtrip_apply"],
            "manager_workbook_apply_action": graph["action_maintenance_manager_workbook_apply"],
            "named": True, "BOARD": admin or graph["page_maintenance_boss"] or graph["page_maintenance"],
            "admin_or_boss": user.role in {"admin", "boss"},
            "maintenance_manager": user.role == "maintenance_manager",
            "ReplenishmentBeta": graph["page_replenishment_beta"],
            "live_F": graph["data_profit"], "live_runtime_safe_F": graph["data_profit"],
            "live_G": graph["data_pool_price_governance"],
            "live_replenishment_create": graph["action_replenishment_create"],
            "explicit_follow_up": bool(stored.get("action_maintenance_collection_follow_up")),
            "explicit_collection_plan_import": bool(stored.get("action_maintenance_collection_plan_import")),
            "explicit_P": graph["page_maintenance"] is True,
            "explicit_C": graph["data_purchase_cost"] is True,
            "explicit_F": graph["data_profit"] is True,
            "explicit_migration_review": graph["action_maintenance_migration_review"] is True,
        }
        for name, definition in predicates.items():
            if isinstance(definition, dict):
                assert definition["evaluator"] == "legacy_require_action"
                data = definition["data_key"]
                values[name] = admin or (bool(graph.get(definition["action"])) and (data is None or bool(graph.get(data))))
        assert set(values) == set(predicates)
        return values

    def user(role="readonly", graph=None, *, snapshot=True, overrides=None):
        return SimpleNamespace(role=role, template_perms=graph if snapshot else None,
                               perm_overrides=overrides or {}, permissions=graph if not snapshot else {})

    cases = []
    for role in perm.ROLE_TEMPLATES:
        default = perm.template_for(role)
        cases.append((f"{role}/base", user(role, default)))
        cases.append((f"{role}/legacy", user(role, default, snapshot=False)))
        for base_name, graph in [("zero", {k: False for k in perm.ALL_KEYS}), ("full", {k: True for k in perm.ALL_KEYS})]:
            cases.append((f"{role}/{base_name}", user(role, graph)))
            for key in perm.ALL_KEYS:
                cases.append((f"{role}/{base_name}/flip/{key}", user(role, graph, overrides={key: not graph[key]})))
    for name, subject in cases:
        values = values_for(subject)
        for r in resources.values():
            actions, scope, fields = seed(r, values)
            assert scope in {x["id"] for x in r["scopes"]}, (name, r["resource_id"], scope)
            for a in r["actions"]:
                if a["id"] in actions:
                    assert set(a["requires_fields"]) <= fields, (name, r["resource_id"], a["id"], fields)
                    assert set(a["requires_actions"]) <= actions, (name, r["resource_id"], a["id"], actions)
                    assert not a["requires_any_actions"] or set(a["requires_any_actions"]) & actions, (name, r["resource_id"], a["id"], actions)
            for source, target in r["field_dependencies"]:
                assert source not in fields or target in fields, (name, r["resource_id"], fields)

    # Concrete independently stated contracts; no admin-F-false invented effective graph.
    def result(r, g, role="readonly"):
        return seed(resources[r], values_for(user(role, g)))
    minimal = {k: False for k in perm.ALL_KEYS}
    minimal["page_maintenance"] = True
    assert result("maintenance_managed_workbook", minimal)[0] == {"download", "validate", "apply", "update_contract_amount"}
    assert result("maintenance_managed_workbook", minimal)[1] == "primary_managed_projects"
    assert result("maintenance_general_workbook", minimal)[0] == set()
    assert result("maintenance_row_workbench", minimal)[0] == {"read_fields", "export"}
    assert result("maintenance_row_workbench", minimal)[2] == set()
    assert result("maintenance_manager_workbook", minimal)[0] == set()
    financial = {**minimal, "data_purchase_cost": True, "data_profit": True, "page_maintenance_beta": True}
    assert result("maintenance_general_workbook_write", financial)[1] == "canonical_sales_projects"
    assert result("maintenance_general_workbook_write", {**financial, "action_maintenance_expense_collection_upload": True})[1] == "assigned_or_sales_projects"
    assert result("maintenance_manager_workbook", financial)[2] == {"data_profit"}
    admin_values = values_for(user("admin", minimal))
    assert admin_values["C"] and admin_values["F"] and admin_values["K"] and not admin_values["Beta"]
    assert not admin_values["explicit_follow_up"] and not admin_values["explicit_collection_plan_import"]
    assert result("maintenance_contract_cost", minimal)[0] == {"read", "export"}
    assert result("maintenance_issue", minimal)[0] == {"read_workspace", "read_demand_candidates"}
    assert result("maintenance_source_health", minimal)[0] == {"read"}
    owner_admin = {**financial, "action_maintenance_collection_plan_import": True}
    assert result("maintenance_collection_plan_import", owner_admin, "admin")[1] == "own_imports"
    assert result("maintenance_collection_plan_import_read", owner_admin, "admin")[1] == "all_imports"

    front = read_json("s3-frontend-manifest-surfaces-fragment.json")
    assert {r["id"] for r in front["resources"]} == set(resources)
    surface_ids = set()
    for r in front["resources"]:
        data_actions = {a["id"] for a in resources[r["id"]]["actions"]}
        fields = set(resources[r["id"]]["field_permissions"])
        display_ids = {a["id"] for a in r["actions"]}
        for action in r["actions"]:
            assert action["kind"] == "display" and action["id"].startswith("show_")
            assert set(action["requires_actions"]) <= data_actions
            assert set(action["requires_any_actions"]) <= data_actions
            assert set(action["requires_fields"]) <= fields
        for surface in r["surfaces"]:
            assert surface["id"] not in surface_ids
            surface_ids.add(surface["id"])
            assert set(surface["display_actions"]) <= display_ids
            for branch in surface["data_rule"]["anyOf"]:
                for requirement in branch:
                    target = resources[requirement["resource_id"]]
                    assert set(requirement["actions"]) <= {a["id"] for a in target["actions"]}
                    assert set(requirement["field_permissions"]) <= set(target["field_permissions"])
    assert set(front["workspace_blocks"]) == {b["id"] for b in spec["workspace"]["blocks"]}
    output = {
        "status": "passed_static_design_checks_only", "resources": len(resources),
        "business_actions": action_count, "data_actions": action_kinds["data"],
        "mutation_actions": action_kinds["mutation"], "human_entries": len(entries), "retired_kept": 11,
        "source_sha256_files": len(sources), "closed_entry_action_references": pair_count,
        "source_observation": source_ref or "working_tree",
        "persisted_legacy_input_vectors": len(cases), "resource_vector_evaluations": len(cases) * len(resources),
        "frontend_surfaces": len(surface_ids), "workspace_blocks": len(spec["workspace"]["blocks"]),
        "closed_collection_refs": len(collection_registry), "exact_collection_purposes": len(collection_usage),
        "base_authorization_resolvers": len(contexts["authorization_view_resolvers"]),
        "input_authorization_resolvers": len(input_registry), "object_types": len(object_types),
        "spec_sha256": hashlib.sha256((HERE / "s3-execution-spec.json").read_bytes()).hexdigest(),
        "frontend_sha256": hashlib.sha256((HERE / "s3-frontend-manifest-surfaces-fragment.json").read_bytes()).hexdigest(),
        "not_proven": ["S3 SQL/PDP adapter", "PG migrations", "concurrent lock behavior", "complete old/new endpoint behavior", "artifact lifecycle"],
    }
    assert spec["counts"]["business_actions"] == action_count
    assert spec["counts"]["data_actions"] == action_kinds["data"]
    assert spec["counts"]["mutation_actions"] == action_kinds["mutation"]
    if emit:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    return output


def self_test(*, source_ref=None):
    """Poison only in-memory copies; never modifies repository or application data."""
    spec = read_json("s3-execution-spec.json")
    main(emit=False, source_ref=source_ref)
    def entry(d, name):
        return next(e for e in d["entries"] if e["entry_id"] == name)
    def resource(d, name):
        return next(r for r in d["resources"] if r["resource_id"] == name)
    bulk = "POST /api/maintenance/spare-part-lines/apply"
    poisons = {
        "source_hash_drift": lambda d: d["supporting_sources"][0].update(sha256="0" * 64),
        "unknown_entry_action": lambda d: entry(d, bulk)["bindings"][0].update(action="unregistered"),
        "unbound_extra_action": lambda d: resource(d, "maintenance_business_date")["actions"].append({"id": "unbound", "kind": "data", "requires_fields": [], "requires_actions": [], "requires_any_actions": []}),
        "contextless_all_scope": lambda d: resource(d, "maintenance_business_date")["scopes"][0].update(requires_context=False),
        "undeclared_field_dependency": lambda d: resource(d, "maintenance_business_date")["actions"][0]["requires_fields"].append("data_profit"),
        "bulk_discovery_as_authority": lambda d: entry(d, bulk)["context_requirement"]["all_of"][0].update(authority="authorized_query"),
        "vacuous_empty_target_admission": lambda d: entry(d, bulk)["context_requirement"]["operation_admission"].update(before_target_quantifier=False),
        "unregistered_input_purpose": lambda d: entry(d, bulk)["context_requirement"]["all_of"][1].update(purpose="arbitrary_client_command"),
        "unbounded_selector": lambda d: entry(d, bulk)["context_requirement"]["all_of"][1]["selector_schema"].update(additionalProperties=True),
        "paged_whole_target_selector": lambda d: entry(d, bulk)["context_requirement"]["all_of"][0]["selector_schema"]["properties"].update(page={"type": "integer"}),
        "warehouse_flattened_branches": lambda d: entry(d, "POST /api/maintenance/warehouse-imports/{import_id}/apply")["context_requirement"].update(quantifier="all_contexts"),
        "collection_plan_lost_targets": lambda d: entry(d, "POST /api/maintenance/collection-plan-imports/{batch_id}/apply")["context_requirement"].update(additional_domain_slots=[]),
    }
    rejected = []
    for name, poison in poisons.items():
        changed = copy.deepcopy(spec)
        poison(changed)
        try:
            main(spec_override=changed, emit=False, source_ref=source_ref)
        except (AssertionError, KeyError, StopIteration):
            rejected.append(name)
        else:
            raise AssertionError(f"Static poison unexpectedly accepted: {name}")
    print(json.dumps({"status": "passed_static_validator_negative_checks_only", "rejected": rejected,
                      "count": len(rejected), "runtime_evidence": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-ref", help="Verify frozen source hashes against this exact git commit; never changes the hashes")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    self_test(source_ref=args.source_ref) if args.self_test else main(source_ref=args.source_ref)
