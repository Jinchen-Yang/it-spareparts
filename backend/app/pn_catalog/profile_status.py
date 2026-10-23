"""Profile confirmation metadata, independent of grade-specific descriptions."""


def advance_fill_status(state, status, *, context):
    if state.fill_status == status:
        return None
    before = state.fill_status
    state.fill_status = status
    state.fill_status_revision += 1
    state.field_provenance = {**state.field_provenance, "fill_status": {
        **context.actor.provenance.model_dump(mode="json", exclude_none=True),
        "principal_id": context.actor.principal_id, "request_id": str(context.request_id),
        "confirmed_by": context.actor.principal_id if status == "confirmed" else None,
    }}
    return {"before": before, "after": status}
