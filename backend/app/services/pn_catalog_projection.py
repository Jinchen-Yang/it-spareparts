"""Restrict known live-master projections without rewriting historical business facts."""
from app.pn_catalog.dto import PROFILE_FIELDS


def project_business_master(data, readable_fields, *, runtime_state, availability):
    readable = frozenset(readable_fields)

    def allowed(field):
        return "*" in readable or field in readable

    def row(value):
        result = dict(value)
        for key in PROFILE_FIELDS | {"category"}:
            if key in result and not allowed("category_major" if key == "category" else key):
                result[key] = None
        if "specs" in result:
            result["specs"] = {key: value for key, value in (result["specs"] or {}).items()
                               if allowed(f"specs.{key}")}
        if "locked_fields" in result:
            result["locked_fields"] = [field for field in (result["locked_fields"] or []) if allowed(field)]
        # The resolver can duplicate description in its match explanation.
        if result.get("match_type") == "description" and not allowed("description"):
            result["matched_text"] = None
        if "match_reason" in result and not all(allowed(field) for field in PROFILE_FIELDS):
            result["match_reason"] = None
        return result

    projected = dict(data)
    for key in ("items", "similar_items", "substitutes"):
        if key in projected:
            projected[key] = [row(item) for item in projected[key]]
    if "part" in projected:
        projected["part"] = row(projected["part"])
    projected["shared_master"] = {"runtime_state": runtime_state, "status": availability,
                                  "readable_fields": sorted(readable)}
    return projected
