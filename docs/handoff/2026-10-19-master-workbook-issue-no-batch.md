# 项目总表领用单号批内冲突修复

- 基线：`origin/main` / `ae32737cb6b5e4a7c125a3192688cc63fc229117`。
- 分支：`fix/workbook-intra-batch-issue-no`；PR 指向 main，不合并、不部署。
- 决策: 无。修复现有项目内单号唯一性与整本原子应用契约，不新增业务口径。
- 无 schema 变更、无迁移、无前端变更；只操作指定独立 worktree 和合成测试库。

## 复现与根因

基线代码的 HTTP 契约测试使用两张不同的既有领用单，将两条签名行的单号同时改为数据库中不存在的 `WBDD-20261013-0005`。validate 返回 200，apply 在服务原第 6851 行 `db.flush()` 抛出真实 PostgreSQL `UniqueViolation / uq_maintenance_site_issue_project_no`，测试失败。它与用户提供的生产错误形状一致；本任务未读取生产数据库或原文件，不能断言已还原生产文件的全部内容。

parse 的 `chosen_headers` 逐张查询数据库原有单号，没有比较本批其他单据的最终单号；两个查询都查不到新号，因而同时通过。apply 的 `requested_headers` 只校验同一 ID 的重复行，随后批量 UPDATE 撞唯一键。失败事务回滚后数据库也不会保留这个目标号。

已核查的相邻路径：

1. 普通互换 A↔B、链式 A→B/B→C 在旧 parse 中会被数据库占号检查拒绝，不能据此认定为本次正常 validate 200 后 500 的根因。绕过 validate 的旧计划则可能在 UPDATE 顺序中撞仍被另一单持有的号。
2. 地址补丁的 `requested_headers.setdefault` 只补该单原号，不能独立让普通互换通过旧 parse。但启用合法互换后，按数据库旧号寻找新增明细的地址归属会写到错误的单头，必须同步修复。
3. 无实体 ID 的普通自填非 WBDD 单号按既有规则表示同头新增明细；“签名 A→B + 新增 B 明细”合法，不能一律按重号拒绝。空单号/WBDD 新行则自动生成独立 LY-AUTO 身份；若另一签名单改成该生成号，旧实现会静默合到同一头，此次按独立身份冲突拒绝。WBDD 原文输入本身不直接占用那个 WBDD 领用单号。
4. 当前只传单头改动的 `apply_locked_site_issue_edits` V2 legacy 调用不自行执行单头 flush、审计或版本推进；本路径由外层写审计、推进版本、统一 flush。稍后带 `requirement_before` 的原语调用仍会进入 corrections 的 flush、审计及版本逻辑。风险还包括新头 INSERT、回执 flush 与最终 commit，故在整个 V2 apply 事务边界转换 `IntegrityError`。

## 实现契约

- parse 与锁内 apply 共用最终占用检查：以稳定 issue_id 区分既有单，以新单身份区分独立 CREATE；未改名的头及作废头继续占号，批内已改走的号可以复用。拒绝信息带单号、Excel 行号以及 `details.rows`。
- 新明细在 parse 收齐最终单号后绑定所属 issue_id，再分配明细序号；新增明细的日期不是改头意图。互换后的地址与明细都按最终归属处理；释放旧号再建新头从第 1 行开始。
- 改号先通过 Core UPDATE 将所有待改号头移至 40 字符 UUID 暂号，再 flush，随后沿用既有原语设置最终号并 flush。强引用保留 ORM 原始头，临时 SQL 不同步 identity map，原语快照、审计及版本推进均保持一次真实改名的语义；事务成功不留暂号。
- 整个 V2 apply 的 `IntegrityError` 先 rollback，再 warning 记录原异常。项目内单号唯一键转 `duplicate_issue_no`，其他约束转 `apply_conflict`；复用 API 既有 WorkbookError 映射，均为 HTTP 422，失败会话可继续查询。

HTTP 错误形状示例（行号和 ID 为示例）：

```json
{
  "detail": {
    "code": "duplicate_issue_no",
    "message": "06_领用返还 第 12、30 行的领用单号 WBDD-20261013-0005 与本项目其他领用单重复，同一项目内两张不同领用单的单号不能重复，请修改其中一张后重试",
    "issues": [],
    "rows": [
      {"row_no": 12, "issue_id": "example-a", "issue_no": "WBDD-20261013-0005"},
      {"row_no": 30, "issue_id": "example-b", "issue_no": "WBDD-20261013-0005"}
    ]
  }
}
```

## 验证与交付

- 新契约文件 `backend/tests/test_master_workbook_issue_no_batch.py`：24 项通过；覆盖真实 HTTP、伪造/重放计划、独立自动新头冲突、互换/链式改名及事实更正、审计/版本/revision、地址/日期/明细序号、校验后占号，以及真实 PostgreSQL 唯一键和其他约束故障。
- flush 故障注入在同一测试事务中用 Core 插入竞争行，证明真实数据库异常转换及整本回滚；不冒充两个并发事务压力测试。
- 完整相关回归：**822 passed、0 failed、0 skipped**。选择所有文件名包含 `master`、`workbook` 或 `site_issue` 的后端测试，共 47 个文件；按排序列表 `[0::3]`、`[1::3]`、`[2::3]` 分组运行。三组分别为 197 passed / 186.20s、347 passed / 351.78s、278 passed / 217.65s，退出码均为 0。仅有既有 openpyxl / TestClient 弃用告警。
- 使用本任务专用 `postgres:15` 容器，随机 localhost 端口；pytest 再为每个进程创建独占数据库，未使用共享 `:5433`。测试结束已移除本任务容器，并核对其不再存在；未更改其他容器。
- 独立代码与需求复核均为同模型不同代理，不宣称异构审查。开发验证不代表用户验收、main 人工审批或生产部署。
- 回退为撤回本次代码提交，无数据库结构回退；已成功的人工改号和业务审计不自动逆向更改。

复跑方式：在独立 PostgreSQL 15 容器就绪后，命令级设置 `PYTEST_DATABASE_BASE_URL` 指向该容器，进入 `backend/` 运行；不要使用其他代理的共享端口。

```bash
uv run --extra dev pytest -q tests/test_master_workbook_issue_no_batch.py
uv run --extra dev python - <<'PY'
from pathlib import Path
import subprocess
import sys

files = sorted(str(p) for p in Path('tests').glob('test_*.py')
               if any(term in p.name for term in ('master', 'workbook', 'site_issue')))
subprocess.run([sys.executable, '-m', 'pytest', '-q', '--tb=short', *files], check=True)
PY
```
