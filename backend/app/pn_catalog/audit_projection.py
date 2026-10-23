"""Project immutable audit changes using current aggregate and field grants."""
from .errors import CatalogError


# Public event facts only. Storage keys, multipart credentials, raw provider
# outputs and arbitrary future event payloads do not become readable by default.
MEDIA_KEYS = frozenset({
    "status", "purpose", "generation", "reason", "asset_id", "entry_id", "part_id",
    "source_entry_id", "source_part_id", "ordered_entry_ids", "primary_entry_id",
    "content_hash", "active", "revision", "recipe", "job_id", "filename", "size_bytes",
    "content_type", "claim_revision", "claimed_by", "released", "takeover_reason",
    "assignment_key", "assignment_intent", "capture_mode", "inbox_id", "position",
    "client_order", "already_grouped", "ordered_inbox_ids", "cancelled",
})


class AuditProjection:
    def __init__(self, db, context, authorize, part_id, master_decision):
        self.db, self.context, self.authorize, self.part_id = db, context, authorize, part_id
        self.cache = {"read": master_decision.readable_fields}

    def fields(self, action):
        from .api import _authorize
        if action not in self.cache:
            try:
                self.cache[action] = _authorize(self.db, self.context, self.authorize, action, self.part_id).readable_fields
            except CatalogError as exc:
                if exc.code != "auth.forbidden" or exc.status_code != 403:
                    raise
                self.cache[action] = frozenset()
        return self.cache[action]

    def granted(self, action, field):
        from .api import _allowed
        return _allowed(self.fields(action), field)

    def changes(self, event):
        from .api import _allowed
        axis = (event.revisions or {}).get("axis", "profile")
        if axis.startswith("media."):
            field = "uploads" if axis == "media.upload" else "inbox" if axis.startswith("media.inbox") else "gallery"
            if not self.granted(field + ".read", field):
                return {}
            if field in {"uploads", "inbox"} and event.principal_id != self.context.actor.principal_id:
                if not self.granted(field + ".manage", field):
                    return {}
            return {k: v for k, v in event.changes.items() if k in MEDIA_KEYS}
        if axis == "identity":
            if not self.granted("identity.read", "identity"):
                return {}
            # Coordinated identity outcomes may contain business facts. Those
            # stay in the independently authorized identity/fact operation.
            return {k: v for k, v in event.changes.items() if k in {"identity", "pn_std", "part_ids", "job_id"}}
        if axis == "external_reference":
            return {k: v for k, v in event.changes.items() if k == "external_reference"}
        projected = {}
        for key, value in event.changes.items():
            if key.startswith("content."):
                field = key.removesuffix(".status") + ".description" if key.endswith(".status") else key
                allowed = self.granted("read_content", field)
            elif key == "attributes":
                allowed = self.granted("read_attributes", "attributes")
            elif key == "aliases":
                allowed = self.granted("identity.read", "aliases")
            else:
                allowed = _allowed(self.fields("read"), key)
            if allowed and axis == "candidate":
                allowed = self.granted("read_candidates", "candidates")
            if allowed:
                projected[key] = value
        return projected
