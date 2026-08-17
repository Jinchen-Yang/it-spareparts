"""screening validator supports auto-review schema v2

Revision ID: d0f2e8c6a4b1
Revises: c9e1a7b3d5f2
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d0f2e8c6a4b1"
down_revision: str | None = "c9e1a7b3d5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALIDATOR_V2 = """
CREATE OR REPLACE FUNCTION replenishment_screening_json_is_valid(payload jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $validator$
SELECT CASE
  WHEN jsonb_typeof(payload) IS DISTINCT FROM 'object' THEN false
  WHEN NOT (payload ?& ARRAY[
    'schema_version', 'as_of', 'lookback_days', 'checks',
    'anomaly_count', 'latest_sales', 'pool_floor_ex_tax'
  ]) THEN false
  WHEN jsonb_typeof(payload->'checks') IS DISTINCT FROM 'array' THEN false
  ELSE
    payload->>'schema_version' IN ('1', '2')
    AND jsonb_typeof(payload->'as_of') = 'string'
    AND payload->>'as_of' ~ '^\\d{4}-\\d{2}-\\d{2}$'
    AND jsonb_typeof(payload->'lookback_days') = 'number'
    AND payload->>'lookback_days' = '182'
    AND jsonb_typeof(payload->'anomaly_count') = 'number'
    AND payload->>'anomaly_count' ~ '^[0-3]$'
    AND jsonb_typeof(payload->'latest_sales') = 'object'
    AND jsonb_typeof(payload->'pool_floor_ex_tax') IN (
      'null', 'number', 'string'
    )
    AND jsonb_array_length(payload->'checks') = 3
    AND (
      SELECT count(*) = 3
        AND count(DISTINCT item->>'key') = 3
        AND bool_and(
          jsonb_typeof(item) IS NOT DISTINCT FROM 'object'
          AND jsonb_typeof(item->'key') IS NOT DISTINCT FROM 'string'
          AND jsonb_typeof(item->'passed') IS NOT DISTINCT FROM 'boolean'
          AND jsonb_typeof(item->'detail') IS NOT DISTINCT FROM 'object'
        )
      FROM jsonb_array_elements(payload->'checks') AS checks(item)
    )
    AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(payload->'checks') AS checks(item)
      WHERE item->>'key' = 'pool_membership'
        AND item->'detail' ?& ARRAY[
          'in_pool', 'pool_name', 'pool_status'
        ]
        AND jsonb_typeof(item#>'{detail,in_pool}') IN (
          'null', 'boolean'
        )
        AND jsonb_typeof(item#>'{detail,pool_name}') IN (
          'null', 'string'
        )
        AND jsonb_typeof(item#>'{detail,pool_status}') IN (
          'null', 'string'
        )
    )
    AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(payload->'checks') AS checks(item)
      WHERE item->>'key' = 'recent_activity'
        AND item->'detail' ?& ARRAY[
          'window', 'purchase_samples', 'sales_samples'
        ]
        AND jsonb_typeof(item#>'{detail,window}') = 'object'
        AND item#>'{detail,window}' ?& ARRAY['from', 'to']
        AND jsonb_typeof(item#>'{detail,window,from}') = 'string'
        AND jsonb_typeof(item#>'{detail,window,to}') = 'string'
        AND jsonb_typeof(item#>'{detail,purchase_samples}') = 'number'
        AND item#>>'{detail,purchase_samples}' ~ '^\\d+$'
        AND jsonb_typeof(item#>'{detail,sales_samples}') = 'number'
        AND item#>>'{detail,sales_samples}' ~ '^\\d+$'
    )
    AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(payload->'checks') AS checks(item)
      WHERE item->>'key' = 'niche_pn'
        AND item->'detail' ?& ARRAY[
          'is_niche', 'purchase_samples', 'sales_samples', 'rule'
        ]
        AND jsonb_typeof(item#>'{detail,is_niche}') = 'boolean'
        AND jsonb_typeof(item#>'{detail,purchase_samples}') = 'number'
        AND item#>>'{detail,purchase_samples}' ~ '^\\d+$'
        AND jsonb_typeof(item#>'{detail,sales_samples}') = 'number'
        AND item#>>'{detail,sales_samples}' ~ '^\\d+$'
        AND jsonb_typeof(item#>'{detail,rule}') = 'string'
    )
    AND (
      payload->>'schema_version' = '1'
      OR (
        payload->>'schema_version' = '2'
        AND jsonb_typeof(payload->'auto_review') IS NOT DISTINCT FROM 'object'
        AND payload->'auto_review'->>'decision' IN ('approved', 'rejected')
        AND jsonb_typeof(payload->'auto_review'->'reason_code')
          IS NOT DISTINCT FROM 'string'
        AND jsonb_typeof(payload->'recommendations') IS NOT DISTINCT FROM 'array'
      )
    )
END
$validator$
"""

_VALIDATOR_V1 = """
CREATE OR REPLACE FUNCTION replenishment_screening_json_is_valid(payload jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $validator$
SELECT CASE
  WHEN jsonb_typeof(payload) IS DISTINCT FROM 'object' THEN false
  WHEN NOT (payload ?& ARRAY[
    'schema_version', 'as_of', 'lookback_days', 'checks',
    'anomaly_count', 'latest_sales', 'pool_floor_ex_tax'
  ]) THEN false
  WHEN jsonb_typeof(payload->'checks') IS DISTINCT FROM 'array' THEN false
  ELSE
    payload->>'schema_version' = '1'
    AND jsonb_typeof(payload->'as_of') = 'string'
    AND payload->>'as_of' ~ '^\\d{4}-\\d{2}-\\d{2}$'
    AND jsonb_typeof(payload->'lookback_days') = 'number'
    AND payload->>'lookback_days' = '182'
    AND jsonb_typeof(payload->'anomaly_count') = 'number'
    AND payload->>'anomaly_count' ~ '^[0-3]$'
    AND jsonb_typeof(payload->'latest_sales') = 'object'
    AND jsonb_typeof(payload->'pool_floor_ex_tax') IN (
      'null', 'number', 'string'
    )
    AND jsonb_array_length(payload->'checks') = 3
    AND (
      SELECT count(*) = 3
        AND count(DISTINCT item->>'key') = 3
        AND bool_and(
          jsonb_typeof(item) IS NOT DISTINCT FROM 'object'
          AND jsonb_typeof(item->'key') IS NOT DISTINCT FROM 'string'
          AND jsonb_typeof(item->'passed') IS NOT DISTINCT FROM 'boolean'
          AND jsonb_typeof(item->'detail') IS NOT DISTINCT FROM 'object'
        )
      FROM jsonb_array_elements(payload->'checks') AS checks(item)
    )
    AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(payload->'checks') AS checks(item)
      WHERE item->>'key' = 'pool_membership'
        AND item->'detail' ?& ARRAY[
          'in_pool', 'pool_name', 'pool_status'
        ]
        AND jsonb_typeof(item#>'{detail,in_pool}') IN (
          'null', 'boolean'
        )
        AND jsonb_typeof(item#>'{detail,pool_name}') IN (
          'null', 'string'
        )
        AND jsonb_typeof(item#>'{detail,pool_status}') IN (
          'null', 'string'
        )
    )
    AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(payload->'checks') AS checks(item)
      WHERE item->>'key' = 'recent_activity'
        AND item->'detail' ?& ARRAY[
          'window', 'purchase_samples', 'sales_samples'
        ]
        AND jsonb_typeof(item#>'{detail,window}') = 'object'
        AND item#>'{detail,window}' ?& ARRAY['from', 'to']
        AND jsonb_typeof(item#>'{detail,window,from}') = 'string'
        AND jsonb_typeof(item#>'{detail,window,to}') = 'string'
        AND jsonb_typeof(item#>'{detail,purchase_samples}') = 'number'
        AND item#>>'{detail,purchase_samples}' ~ '^\\d+$'
        AND jsonb_typeof(item#>'{detail,sales_samples}') = 'number'
        AND item#>>'{detail,sales_samples}' ~ '^\\d+$'
    )
    AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(payload->'checks') AS checks(item)
      WHERE item->>'key' = 'niche_pn'
        AND item->'detail' ?& ARRAY[
          'is_niche', 'purchase_samples', 'sales_samples', 'rule'
        ]
        AND jsonb_typeof(item#>'{detail,is_niche}') = 'boolean'
        AND jsonb_typeof(item#>'{detail,purchase_samples}') = 'number'
        AND item#>>'{detail,purchase_samples}' ~ '^\\d+$'
        AND jsonb_typeof(item#>'{detail,sales_samples}') = 'number'
        AND item#>>'{detail,sales_samples}' ~ '^\\d+$'
        AND jsonb_typeof(item#>'{detail,rule}') = 'string'
    )
END
$validator$
"""


def upgrade() -> None:
    op.execute(_VALIDATOR_V2)


def downgrade() -> None:
    op.execute(_VALIDATOR_V1)
