"""Optional contact data is inherited only from the actual frozen legacy bit."""
import json
from sqlalchemy import text
from tests.test_authz_s3_migration import migrate,insert,S2,S3


def test_real_migration_demand_contact_bit_does_not_become_read_dependency(db):
    migrate(db,S2,down=True)
    try:
        ids=[]
        for enabled in (False,True):
            uid=insert(db,'demand-contact-'+str(enabled),role='readonly')
            db.execute(text('UPDATE sys_user SET template_perms=CAST(:p AS jsonb) WHERE id=:id'),{'id':uid,
                'p':json.dumps({'page_maintenance':True,'page_maintenance_beta':True,'data_customer':enabled})})
            ids.append(uid)
        migrate(db,S3)
        assert db.scalar(text('SELECT version_num FROM alembic_version')) == S3
        for uid,enabled in zip(ids,(False,True)):
            row=db.execute(text("SELECT actions,field_permissions FROM sys_authorization_grant WHERE user_id=:uid AND resource='maintenance_demand'"),{'uid':uid}).one()
            assert 'read' in row.actions
            assert ('data_customer' in row.field_permissions)==enabled
    finally:
        db.rollback();migrate(db,'head')
