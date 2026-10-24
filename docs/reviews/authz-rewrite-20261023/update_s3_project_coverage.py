import json,pathlib
root=pathlib.Path(__file__).resolve().parents[3];docs=root/'docs/reviews/authz-rewrite-20261023';spec=json.loads((docs/'s3-execution-spec.json').read_text())
files={'maintenance','maintenance_analytics','maintenance_audit','maintenance_boss_board','maintenance_collection_evidence','maintenance_collection_plan_imports','maintenance_collection_reminders','maintenance_dashboard','maintenance_expense_reconcile','maintenance_front_stock','maintenance_project_operations','maintenance_projects','maintenance_recovery'}
verified={
 'GET /api/maintenance/projects/stable':('maintenance_project_access.read_directory','project_condition before q/count/page','test_authz_s3_projects.py'),
 'POST /api/maintenance/projects/stable/search':('maintenance_project_access.read_directory','project_condition before q/count/page','test_authz_s3_projects.py'),
 'GET /api/maintenance/projects/stable/{project_id}':('maintenance_project_access.read_overview','real project context before projected facts','test_authz_s3_projects.py'),
 'PATCH /api/maintenance/projects/stable/{project_id}':('maintenance_project_access.change_project','catalog/project/assignment locks then final sorted users; recheck before write','test_authz_s3_projects.py'),
 'POST /api/maintenance/projects/stable/{project_id}/archive':('maintenance_project_access.change_project','catalog/project/assignment locks then final sorted users','test_authz_s3_projects.py'),
 'POST /api/maintenance/projects/stable/{project_id}/restore':('maintenance_project_access.change_project','catalog/project/assignment locks then final sorted users','test_authz_s3_projects.py'),
}
wired={
 'GET /api/maintenance/projects/stable/{project_id}/front-stock':('maintenance_project_read_access.read_front_stock','same RR object check and cost projection'),
 'GET /api/maintenance/projects/stable/{project_id}/recovery-summary':('maintenance_project_read_access.read_recovery','same RR object check and cost projection'),
 'GET /api/maintenance/projects/stable/{project_id}/workspace':('maintenance_project_read_access.read_workspace','six independent resources; denied block facts short circuit; own fields; same RR'),
}

for eid,(facade,boundary) in wired.items():
 verified[eid]=(facade,boundary,'test_authz_s3_workspace.py')
for suffix,resource,facade in [('tasks','maintenance_project_operations','read_tasks'),('purchases','maintenance_project_purchase','read_purchases'),('cost-gaps','maintenance_cost_fill','read_cost_gaps')]:
 verified['GET /api/maintenance/projects/stable/{project_id}/'+suffix]=('maintenance_project_read_access.'+facade,'same RR actual project; own resource fields; independent action','test_authz_s3_workspace.py')
for path,facade in [('as-of','read_business_date'),('match-audit','read_match_audit'),('reconcile/expenses','read_expense_reconcile')]:
 verified['GET /api/maintenance/'+path]=('maintenance_diagnostics_access.'+facade,'registered global collection; live explicit action before diagnostic facts','test_authz_s3_diagnostics.py')
for eid in ['POST /api/maintenance/projects/stable/{project_id}/collections','PATCH /api/maintenance/projects/stable/collections/{collection_id}','POST /api/maintenance/projects/stable/{project_id}/expenses','PUT /api/maintenance/projects/stable/{project_id}/expenses/readiness']:
 verified[eid]=('maintenance_finance_access.execute','actual parent/input proof; full domain locks then actor SHARE; live recheck; projection before commit','test_authz_s3_finance.py')
for eid in ['GET /api/maintenance/projects/stable/operations','POST /api/maintenance/projects/stable/operations/search']:
 verified[eid]=('maintenance_project_access.read_operations_directory','project predicate in each financial CTE before aggregate/count/page; hidden field filters denied','test_authz_s3_workspace.py')
for suffix in ['pn-ranking','spend-trend','filter-options','explorer']:
 verified['GET /api/maintenance/analytics/'+suffix]=('maintenance_analysis_access.read','Plan SQL before aggregate/count/page; same RR fields/filters; all/unassigned only unconditional actual plan','test_authz_s3_analysis.py')
for suffix in ['options','summary','projects','parts','valuation','receipts','quantity-checks','orders','consumption','consumption/issues']:
 verified['GET /api/maintenance/analytics/maintenance-dashboard/'+suffix]=('maintenance_dashboard_access.read','Plan SQL before source/issue/receipt facts; same RR independent action and own F projection','test_authz_s3_dashboard.py')
verified['GET /api/maintenance/analytics/maintenance-dashboard/export']=('maintenance_dashboard_access.prepare_export','kind read AND export; sameRR SQL/field materialization; generated schema2 lineage; headers and each chunk revalidate','test_authz_s3_dashboard.py')
for eid in ['PATCH /api/maintenance/projects/stable/expenses/{expense_id}/status','PATCH /api/maintenance/projects/stable/{project_id}/cost-gaps','POST /api/maintenance/projects/stable/{project_id}/cost-gaps/recompute']:
 verified[eid]=('maintenance_finance_access.execute','actual child/current parent before lookup; complete domain locks then actor SHARE; recheck before projection/commit','test_authz_s3_finance.py')
for suffix in ['health','summary','attention','projects','projects/{project_id}']:
 verified['GET /api/maintenance/boss-board/'+suffix]=('maintenance_board_access.read','same RR; exact independent action; project Plan before facts/count/page; own C/F/K column projection','test_authz_s3_board.py')
verified['POST /api/maintenance/boss-board/projects/search']=('maintenance_board_access.read','project Plan before search/count/page; financial filters use card Grant only','test_authz_s3_board.py')
for eid in ['GET /api/maintenance/boss-board/orders/{source_order_id}/lines','POST /api/maintenance/boss-board/projects/export','GET /api/maintenance/boss-board/projects/export/options','GET /api/maintenance/boss-board/projects/{project_id}/orders']:
 verified[eid]=('maintenance_board_access.read/prepare_export','actual parent/source or registered unassigned branch; all only actual PDP; hidden columns not selected; rendered export current provenance recheck','test_authz_s3_board.py')
verified['POST /api/maintenance/projects/stable']=('fixed canonical D05 rejection','409 regardless of admin/full grants; no project/alias/audit writes; never-cutover legacy compatibility','test_authz_s3_projects.py')
for eid in ['POST /api/maintenance/projects/stable/{project_id}/contracts','PATCH /api/maintenance/projects/stable/contracts/{project_contract_id}','POST /api/maintenance/projects/stable/contracts/{project_contract_id}/archive']:
 verified[eid]=('maintenance_finance_access.execute','complete resync domain closure before actor SHARE; locked current grant and own fields; atomic failure','test_authz_s3_finance.py')
for suffix in ['preview','apply']:
 verified[('GET' if suffix=='preview' else 'POST')+' /api/maintenance/projects/stable/business-type-backfill/'+suffix]=('maintenance_project_backfill_access.preview/apply','verified complete command-plan targets; exact empty admission; domain before actor locks','test_authz_s3_project_backfill.py')
for eid in ['GET /api/maintenance/projects','GET /api/maintenance/lines','GET /api/maintenance/export','GET /api/maintenance/lines/export','GET /api/maintenance/board','GET /api/maintenance/board/export']:
 verified[eid]=('maintenance_cost_access.read/prepare_export','source-sales SQL predicate before aggregate/page/count; hidden costs never selected; same RR materialization and streamed provenance checks','test_authz_s3_source_cost.py')
for eid in ['GET /api/maintenance/collection-milestones/{milestone_id}/evidence','POST /api/maintenance/collection-milestones/{milestone_id}/evidence','POST /api/maintenance/collection-milestones/{milestone_id}/follow-ups','POST /api/maintenance/collection-reminders/search','GET /api/maintenance/projects/stable/{project_id}/collection-milestones']:
 verified[eid]=('maintenance_collection_followup_access.read/record/evidence_read/evidence_upload','project plan before count/page; own planned-amount projection; actual milestone/contract/parent plus bytes-bound upload; domain locks then actor and atomic close','test_authz_s3_collection_followup.py')
for eid in ['POST /api/maintenance/collection-plan-imports/preview','POST /api/maintenance/collection-plan-imports/{batch_id}/apply','GET /api/maintenance/collection-plan-imports/{batch_id}/binding-options','GET /api/maintenance/collection-plan-imports/{batch_id}/source-file']:
 verified[eid]=('maintenance_collection_plan_access.preview/apply/read','bytes and complete immutable plan sidecar; current independent owner/all reader contract; whole-plan domain locks then actor SHARE; atomic files+metadata rollback','test_authz_s3_collection_plan.py')
for eid in ['POST /api/maintenance/site-issues/search','POST /api/maintenance/site-issues/projects/{project_id}/candidates/search','GET /api/maintenance/site-issues/projects/{project_id}/demand-candidates','POST /api/maintenance/site-issues/{issue_id}/preview','POST /api/maintenance/site-issues/manual/preview','POST /api/maintenance/projects/stable/{project_id}/site-issues','POST /api/maintenance/site-issues/projects/{project_id}','POST /api/maintenance/site-issues/{issue_id}/confirm','PATCH /api/maintenance/site-issues/{issue_id}','POST /api/maintenance/site-issues/{issue_id}/void','PATCH /api/maintenance/projects/stable/site-issues/{issue_id}/status','POST /api/maintenance/site-issues/projects/{project_id}/manual','PATCH /api/maintenance/site-issues/manual/{issue_id}','PATCH /api/maintenance/site-issues/{issue_id}/header','PATCH /api/maintenance/site-issues/{issue_id}/lines/{issue_line_id}']:
 verified[eid]=('maintenance_site_issue_access.read/execute','exact actual project and issue/line metadata; same RR facts; stable command key; all pending domain effects before final actor SHARE/PDP; revoke rolls back issue/line/receipt','test_authz_s3_site_issue.py')
for eid in ['POST /api/maintenance/projects/stable/site-issue-costs/backfill','POST /api/maintenance/projects/stable/expense-attribution/backfill']:
 verified[eid]=('maintenance_operation_backfill_access.execute','complete global target metadata and input digest; every actual old/new parent; no-parent real subject not empty; domain locks then final actor; late revoke full rollback','test_authz_s3_operation_backfills.py')
for eid in ['GET /api/maintenance/export-workbook','GET /api/maintenance/export-workbooks','GET /api/maintenance/orders/export','GET /api/maintenance/roundtrip-template','GET /api/maintenance/roundtrip-templates']:
 verified[eid]=('maintenance_global_workbook_authorization.read','exact full-source field contract; SQL before workbook rendering in one RR; bounded complete spool; current grant and real source relations rechecked before headers and each chunk','test_authz_s3_global_workbooks.py')
verified['POST /api/maintenance/recompute']=('maintenance_global_workbook_authorization.recompute','actual full global source and site closure; all domain work before final actor/PDP; late revocation rolls back facts','test_authz_s3_global_workbooks.py')
verified['POST /api/maintenance/roundtrip-import']=('maintenance_roundtrip_authorization.apply','actual bytes + full intents and global recompute closure; stable owner receipt with immutable operations and raw file; final actor/PDP before commit; atomic archive cleanup','test_authz_s3_roundtrip.py')
independent={
 eid:{'report':'docs/reviews/authz-rewrite-20261023/s3-global-independent-review.md','test_files':['backend/tests/test_authz_s3_global_independent.py'],'assertions':['post-render current Grant/fields revoked rejects release','post-render actual source reparent invalidates proof','denied release closes complete spool']}
 for eid in ['GET /api/maintenance/export-workbook','GET /api/maintenance/export-workbooks','GET /api/maintenance/orders/export','GET /api/maintenance/roundtrip-template','GET /api/maintenance/roundtrip-templates','POST /api/maintenance/recompute']}
independent['POST /api/maintenance/roundtrip-import']={'report':'docs/reviews/authz-rewrite-20261023/s3-roundtrip-independent-review.md','test_files':['backend/tests/test_authz_s3_roundtrip_independent.py'],'assertions':['actual raw metadata tamper rejects replay','second publication failure preserves first committed archive','field-only revocation before commit rolls back all facts and receipt']}
entries=[]
for e in spec['entries']:
 if pathlib.Path(e['source']['file']).stem not in files:continue
 eid=e['entry_id'];v=verified.get(eid);w=wired.get(eid)
 entries.append({'entry_id':eid,'source':e['source']['file'],'status':'semantic_tests_passed' if v else 'wired_pending_semantic_validation' if w else 'pending_implementation','facade':(v or w or (None,))[0],'query_or_lock_boundary':(v or w or (None,None))[1],'semantic_test_file':'backend/tests/'+v[2] if v else None,'bindings':e['bindings'], 'independent_review':independent.get(eid)})
result={'scope':'project_read_finance','expected_total':94,'actual_total':len(entries),'meaning':'Registry metadata is not implementation evidence. Pass status requires live PG/PDP HTTP/SQL or concurrency tests; pending remain open. No production or CI completion claim.','additional_shared_facades':[{'facade':'maintenance_project_assignments.assignment_command','entries':['POST /api/maintenance/projects/stable/{project_id}/manager-assignment','POST /api/maintenance/project-manager-assignments/{assignment_id}/archive'],'tests':'backend/tests/test_authz_s3_assignment_commands.py','status':'semantic_tests_passed'},{'facade':'maintenance_project_assignments.read_manager_directory','entry':'POST /api/maintenance/project-manager-assignments/search','status':'semantic_tests_passed','tests':'backend/tests/test_authz_s3_assignment_commands.py'}],'entries':entries}
assert len(entries)==94,len(entries)
(docs/'s3-project-read-finance-implementation-coverage.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(len(entries))
