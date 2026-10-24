"""The three S3 maintenance tools reuse their actual HTTP protected read contract."""
from types import MappingProxyType

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import event

from app.agent import tools
from app.authz.principal import VerifiedIdentity
from app.authz.provenance import AgentAccess, execution_access
from app.models.authorization import SysAuthorizationGrant
from app.security import UserContext
from app.services.agent_authorization import ExecutionContract, CHAT
from tests.test_authz_s3_source_cost import source_cost, give  # noqa: F401
from tests.test_authz_s3_analysis import analysis_client  # noqa: F401
from tests.test_authz_s3_workspace import workspace  # noqa: F401
from tests.test_authz_s3_projects import canonical  # noqa: F401
from tests.test_authz_s3_project_foundation import projects  # noqa: F401
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.mark.parametrize('tool,action,resource,scope,path,args', [
    ('get_maintenance_projects', 'read_projects', 'maintenance_cost', 'own_source_sales', '/api/maintenance/projects', {}),
    ('get_maintenance_lines', 'read_lines', 'maintenance_cost', 'own_source_sales', '/api/maintenance/lines', {'project': 'same-source-project'}),
    ('get_maintenance_board', 'read', 'maintenance_contract_cost', 'all_contracts', '/api/maintenance/board', {}),
])
def test_tool_and_http_have_the_same_scoped_field_projected_result(db, source_cost, tool, action, resource, scope, path, args):
    client, headers, actor = source_cost
    give(db, actor, [action], resource=resource, scope=scope)
    db.add(SysAuthorizationGrant(user_id=actor.id, resource='agent_chat', actions=['use'], scope='self', field_permissions=[], version=1))
    db.commit()
    identity = VerifiedIdentity(actor.username, actor.token_version, False, 'sys_user', MappingProxyType({}), actor.id)
    access = AgentAccess(db.get_bind(), identity)
    access.bind_execution(ExecutionContract(CHAT, {'messages': [{'role': 'user', 'content': 'read authorized maintenance'}]}))
    observed = []
    def sql(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().lower().startswith('select'):
            observed.append(statement.lower())
    event.listen(db.get_bind(), 'before_cursor_execute', sql)
    try:
        with execution_access(access):
            result = tools.dispatch(db, tool, args, UserContext(user_id=actor.username, role='readonly'))
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', sql)
    assert 'error' not in result, result
    http = client.get(path, params=args, headers=headers)
    assert http.status_code == 200, http.text
    assert jsonable_encoder(result) == http.json()
    assert [(r['resource_id'], r['actions']) for r in access.snapshot()['resources']] == [(resource, [action])]
    assert all(not any(column in statement for column in ('.cost_amount', '.unit_cost', '.amount_inc_tax',
        '.receiver_phone', '.receiver_address')) for statement in observed)
