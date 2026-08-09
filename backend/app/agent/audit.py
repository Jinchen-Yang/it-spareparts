"""Value-free structural audit metadata for Agent requests."""


def chat_request_shape(
    *,
    message_count: int,
    last_message: str,
    endpoint: str,
    stream: bool,
    session_id: int | None = None,
) -> dict:
    """Return bounded structural facts without copying prompt text into access logs."""
    shape = {
        "message_count": max(int(message_count), 0),
        "last_message_chars": len(last_message),
        "endpoint": endpoint,
        "stream": stream,
    }
    if session_id is not None:
        shape["session_id"] = session_id
    return shape
