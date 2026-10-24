"""A source write permit binds a checked action to one real ETL transaction."""
from dataclasses import asdict
from hashlib import sha256
import json

from app.authz.service import ResourceAccess


def _plan_digest(value):
    return sha256(json.dumps(asdict(value),sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()


class SourceImportPermit:
    def __init__(self, db, access, *, source, content_sha256, mode):
        if (type(access) is not ResourceAccess or access.resource!='import_'+source
                or access.action!='submit_'+mode or access.scope!='all'
                or access.object_ref is None or access.entry_id not in (
                    'POST /api/import/upload','WORKER import.execute_item')):
            raise ValueError('源写许可必须来自本次完整实际导入动作')
        self.session=db;self.transaction=db.get_transaction();self.access=access
        self.source=source;self.content_sha256=content_sha256;self.mode=mode
        self.created_batch=None;self.plan_digest=None;self.loaded=False
        if self.transaction is None:raise ValueError('源写许可必须绑定当前事务')

    def check(self,db):
        if (db is not self.session or db.get_transaction() is not self.transaction
                or not self.transaction.is_active):
            raise ValueError('源写许可不能跨会话或事务复用')

    def bind_batch(self,db,batch):
        self.check(db)
        if self.created_batch is not None:raise ValueError('源写许可不能创建第二回执')
        self.created_batch=batch

    def bind_loaded(self,db,loaded,content_sha256,mode):
        self.check(db)
        actual='expense' if loaded.expense_sheets else loaded.file_type
        if self.loaded or actual!=self.source or content_sha256!=self.content_sha256 or mode!=self.mode:
            raise ValueError('实际源文件与已授权输入不一致')
        self.loaded=True

    def bind_plan(self,db,plan):
        self.check(db)
        if not self.loaded or self.plan_digest is not None or plan.file_type!=self.source:
            raise ValueError('实际源计划与已授权来源不一致')
        self.plan_digest=_plan_digest(plan)

    def require_source(self,db,source,plan):
        self.check(db)
        if (source!=self.source or self.plan_digest is None or plan.file_type!=self.source
                or _plan_digest(plan)!=self.plan_digest):
            raise ValueError('派生工作必须来自同事务已验证且未变化的真实来源计划')
