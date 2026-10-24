"""Generate browser vocabulary from the executable registry, never from user grants.

Run from any directory with backend's Python environment. --check is read-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.authz.catalog import RESOURCE_DEFINITIONS, resource_descriptors, validate_catalog
from app.authz.context_registry import (
    CLOSED_KINDS, COLLECTION_REFS, ENTRY_CONTRACTS, AUTHORIZATION_VIEWS,
)

OUTPUT = BACKEND.parent / "frontend/src/authz/generated.ts"


def generate() -> str:
    validate_catalog()
    descriptors = resource_descriptors()
    known = {m["id"]: m["migration_state"] for m in descriptors}
    executable = {}
    surfaces = {}
    for module in descriptors:
        resource = module["id"]
        sources = module.get("legacy_sources")
        if (not isinstance(sources, list) or any(not isinstance(source, str) for source in sources)
                or len(set(sources)) != len(sources)):
            raise ValueError(f"Explicit legacy_sources string array required: {resource}")
        if resource not in RESOURCE_DEFINITIONS:
            continue
        definition = RESOURCE_DEFINITIONS[resource]
        executable[resource] = {
            "actions": {a["id"]: {k: a[k] for k in (
                "kind", "requires_actions", "requires_any_actions", "requires_fields"
            )} for a in module["actions"]},
            "fields": {f["id"]: f["requires_fields"] for f in module["field_permissions"]},
            "scopes": {s["id"]: {"requires_context": s["requires_context"]} for s in module["scopes"]},
            "denial_scope": definition.denial_scope,
        }
        for surface in module["surfaces"]:
            if surface["id"] in surfaces:
                raise ValueError("Surface IDs must be globally unique")
            if type(surface.get("route_entry")) is not bool:
                raise ValueError(f"Explicit route_entry required: {surface['id']}")
            routes = surface.get("page_routes")
            if (not isinstance(routes, list) or (surface["route_entry"] and not routes)
                    or len(set(routes)) != len(routes)
                    or any(not isinstance(p, str) or not p.startswith("/") for p in routes)):
                raise ValueError(f"Closed page_routes required: {surface['id']}")
            rule = surface.get("data_rule")
            if not isinstance(rule, dict) or not isinstance(rule.get("anyOf"), list) or not rule["anyOf"]:
                raise ValueError(f"Nonempty data_rule required: {surface['id']}")
            for clause in rule["anyOf"]:
                if not isinstance(clause, list) or not clause:
                    raise ValueError("Empty rule clause cannot grant access")
                for need in clause:
                    other = RESOURCE_DEFINITIONS.get(need["resource_id"])
                    if other is None or not set(need["actions"]) <= other.actions or not set(need["field_permissions"]) <= other.field_permissions:
                        raise ValueError("Executable surface references an unknown requirement")
                    other_metadata = next(m for m in descriptors if m["id"] == need["resource_id"])
                    if any(a["kind"] == "display" and a["id"] in need["actions"] for a in other_metadata["actions"]):
                        raise ValueError("Display actions cannot authorize transport data")
            surfaces[surface["id"]] = {"resource_id": resource, **{
                k: surface[k] for k in ("display_actions", "page_routes", "route_entry", "data_rule")
            }}
    context_entries = {
        key: {
            "branches": value["branches"],
            "slots": {
                slot: {field: data[field] for field in (
                    "kind", "object_type", "collection_ref", "purpose", "authorizing"
                ) if field in data}
                for slot, data in value["slots"].items()
            },
        }
        for key, value in ENTRY_CONTRACTS.items()
    }
    context_views = {key: {field: value[field] for field in (
        "method", "path", "view", "only_entry_actions", "no_business_payload",
        "authorization_requirements", "target_entry_ids", "locator_path_parameter",
    ) if field in value} for key, value in AUTHORIZATION_VIEWS.items()}
    if not set(context_views) <= set(context_entries):
        raise ValueError("Authorization view has no executable entry contract")
    payload = {"known": known, "resources": executable, "surfaces": surfaces,
               "contexts": {"kinds": CLOSED_KINDS, "collections": COLLECTION_REFS,
                            "entries": context_entries, "views": context_views}}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    dump = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    return (
        "// Generated by backend/scripts/generate_authz_frontend.py. Do not edit.\n"
        "// Descriptors are vocabulary, never actor authorization.\n"
        "export const AUTHZ_PROTOCOL_VERSION = 1 as const;\n"
        f'export const AUTHZ_VOCABULARY_HASH = "{hashlib.sha256(canonical.encode()).hexdigest()}";\n'
        f"export const KNOWN_MODULES = {dump(known)} as const;\n"
        f"export const EXECUTABLE_RESOURCES = {dump(executable)} as const;\n"
        f"export const EXECUTABLE_SURFACES = {dump(surfaces)} as const;\n"
        f"export const CONTEXT_KINDS = {dump(CLOSED_KINDS)} as const;\n"
        f"export const CONTEXT_COLLECTION_REFS = {dump(COLLECTION_REFS)} as const;\n"
        f"export const CONTEXT_ENTRY_CONTRACTS = {dump(context_entries)} as const;\n"
        f"export const CONTEXT_AUTHORIZATION_VIEWS = {dump(context_views)} as const;\n"
        "export type ResourceId = keyof typeof EXECUTABLE_RESOURCES;\n"
        "export type ActionId<R extends ResourceId> = keyof typeof EXECUTABLE_RESOURCES[R]['actions'];\n"
        "export type FieldId<R extends ResourceId> = keyof typeof EXECUTABLE_RESOURCES[R]['fields'];\n"
        "export type ScopeId<R extends ResourceId> = keyof typeof EXECUTABLE_RESOURCES[R]['scopes'];\n"
        "export type SurfaceId = keyof typeof EXECUTABLE_SURFACES;\n"
        "export type ContextKind = typeof CONTEXT_KINDS[number];\n"
        "export type CollectionRef = typeof CONTEXT_COLLECTION_REFS[number];\n"
        "export type ContextEntryId = keyof typeof CONTEXT_ENTRY_CONTRACTS;\n"
        "export type AuthorizationViewId = keyof typeof CONTEXT_AUTHORIZATION_VIEWS;\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = generate()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != result:
            sys.exit("Authorization frontend vocabulary is stale; run backend/scripts/generate_authz_frontend.py")
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(result)
