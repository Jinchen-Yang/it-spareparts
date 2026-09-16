"""Permission-filtered WBDD contact fields for read responses only."""

from app.models.maintenance import FMaintenanceOrder
from app.security import UserContext, is_field_hidden


def order_contact(order: FMaintenanceOrder, user_ctx: UserContext | None) -> dict:
    # Use the customer group's existing effective-context / legacy-template rules.
    # No context is fail-closed; restricted rows never reveal whether data exists.
    visible = user_ctx is not None and not is_field_hidden(user_ctx, "customer_name")
    return {
        "contact_info_state": "visible" if visible else "restricted",
        "receiver_address": order.receiver_address if visible else None,
        "receiver": order.receiver if visible else None,
        "receiver_phone": order.receiver_phone if visible else None,
    }
