"""Agent-wide resource ceilings.

These constants are the single enforcement contract shared by provider assembly, runtime,
session projection and egress metadata. They are code policy rather than deployment tuning: a
provider or alternate runtime cannot raise them through environment variables.
"""

MIB = 1024 * 1024

# Provider tool-call assembly and runtime amplification ceilings.
MAX_TOOL_CALLS_PER_RESPONSE = 16
MAX_TOOL_CALLS_PER_RUN = 32
MAX_TOOL_ARGUMENT_BYTES_PER_CALL = 8 * MIB
MAX_TOOL_ARGUMENT_BYTES_PER_RESPONSE = 16 * MIB
MAX_TOOL_CALL_ID_CHARS = 256
MAX_TOOL_NAME_CHARS = 128

# User-visible answer ceilings (provider response and cumulative multi-round run/session).
# Large tabular/report output belongs in an ACL-protected Artifact.  Keeping chat text at 512 KiB
# bounds the number of live identity/policy refreshes an adversarial provider can amplify.
MAX_VISIBLE_RESPONSE_BYTES = 512 * 1024
MAX_VISIBLE_RUN_BYTES = 512 * 1024

# Raw provider response ceilings are enforced by the transport before the OpenAI SDK parses
# either streamed or non-streamed JSON.  The primary ceiling leaves bounded JSON envelope room
# around the independently capped visible text/tool arguments; Vision only returns OCR text.
PRIMARY_PROVIDER_RESPONSE_MAX_BYTES = 20 * MIB
VISION_PROVIDER_RESPONSE_MAX_BYTES = 3 * MIB
MAX_PROVIDER_RESPONSE_CHUNKS = 65_536
MAX_PROVIDER_REQUEST_HEADER_BYTES = 8 * 1024
VISION_RENDER_MAX_PIXELS_PER_PAGE = 12_000_000
VISION_RENDER_MAX_PIXELS_TOTAL = 48_000_000
VISION_RENDER_MAX_SECONDS = 30.0

# Visible provider tokens are coalesced before they become runtime/SSE/checkpoint events.  This
# caps per-run re-authorization, fan-out and persistence work independently of the byte ceiling.
FIRST_STREAM_DELTA_BATCH_BYTES = 8 * 1024
STREAM_DELTA_BATCH_BYTES = 8 * 1024
MAX_PUBLIC_DELTA_EVENTS = 64

# Public telemetry/checkpoint projection ceilings.
MAX_PUBLIC_TRACE_ENTRIES = MAX_TOOL_CALLS_PER_RUN
MAX_ARTIFACT_IDS_PER_TRACE_ENTRY = 8
MAX_PUBLIC_ARG_KEYS = 64

# Declared egress-edge wire ceilings.
BUSINESS_RESULT_MAX_BYTES = 2 * MIB
CUSTOMER_FILE_RESULT_MAX_BYTES = 4 * MIB
CONVERSATION_CONTEXT_MAX_BYTES = 8 * MIB
VISION_INPUT_MAX_BYTES = 20 * MIB
VISION_OCR_RESULT_MAX_BYTES = 2 * MIB
