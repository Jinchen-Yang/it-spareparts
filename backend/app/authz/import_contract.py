"""Closed generic-import vocabulary shared by adapters and wire generators."""
SOURCES = ('purchase', 'sales', 'inventory', 'maintenance', 'expense')
PARSER_VERSION = 1
PROTOCOL = 'authz_s4'
JOB_ITEM_CODES = (
    'IMPORT_CONTENT_INVALID', 'IMPORT_DUPLICATE', 'IMPORT_CONFLICT',
    'IMPORT_VOID_PREVIEW_REQUIRED', 'IMPORT_VOID_PREVIEW_STALE',
    'IMPORT_STORAGE_UNAVAILABLE', 'IMPORT_EXECUTION_FAILED',
    'AUTHN_SESSION_INVALID', 'AUTHZ_FORBIDDEN', 'AUTHZ_CONTEXT_STALE',
    'AUTHZ_ARTIFACT_STALE', 'AUTHZ_RESOURCE_UNAVAILABLE', 'AUTHZ_ENGINE_UNAVAILABLE',
)
JOB_ITEM_STATE_CODES = {
    'queued': (None,), 'processing': (None,), 'success': (None,), 'partial': (None,),
    'failed': JOB_ITEM_CODES[:7], 'authorization_failed': JOB_ITEM_CODES[7:],
}
PRECHECK = 'POST /api/import/precheck'
UPLOAD = 'POST /api/import/upload'
UPLOAD_BATCH = 'POST /api/import/upload-batch'
VOID = 'POST /api/import/expense-void-preview'
JOBS = 'GET /api/import/jobs'
JOB = 'GET /api/import/jobs/{job_id}'
BATCHES = 'GET /api/import/batches'
BATCH = 'GET /api/import/batches/{batch_id}'
ERRORS = 'GET /api/import/batches/{batch_id}/errors.csv'
WORKER = 'WORKER import.execute_item'
HTTP_ENTRIES = (PRECHECK, UPLOAD, UPLOAD_BATCH, VOID, JOBS, JOB, BATCHES, BATCH, ERRORS)
