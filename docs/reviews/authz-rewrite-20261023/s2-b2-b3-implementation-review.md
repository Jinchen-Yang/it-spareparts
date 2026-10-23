# S2 B2/B3 implementation and verification

Status: local implementation and targeted verification; no production deployment, merge, or independent full-CI claim. The parent integrates the shared core, migration, Agent and frontend packages separately.

## Implemented boundary

The 17 resources are served by four protected application modules: `company_access`, `pool_access`, `part_access`, `governance_access`. HTTP handlers and the part tools call these application services; the calculation/domain modules do not import HTTP handlers. Only resources still in `legacy` use the legacy adapter. Any canonical composite requires all of its registered resources to be enabled; a suspended or partially enabled composite fails as a whole. Missing Grants/actions produce individual deny blocks; PDP/invalid-state failures are not converted into empty allowed results.

| Resource group | Resources | Important domain projection |
| --- | --- | --- |
| Company | company_summary, company_sales_orders, company_purchase_orders, company_pool_analysis, price_discipline, company_pool_order_facts | Summary, transaction and cross-brand blocks have separate actions. Customer filters require the actual customer field. Order facts do not borrow the regular pool facts Grant. |
| Pools | pool_price_analysis, pool_order_facts, pool_catalog, pool_price_policy | Analysis and transaction aliases are projected with their respective Grants. Policy, policy history and coverage use only the policy Grant plus its governance field. Denied policy queries do not run. |
| Parts | part_lookup, part_master, part_substitute | Overview and quick-pricing accept independent actual blocks. `part=null` when identity read is denied. Direct sales/purchase history resolves routing columns only. Master search has a fixed separate endpoint; browse cannot change authorization resource. Substitute inventory additionally needs inventory_read. |
| Governance | governance, part_identity, data_quality, data_quality_calibration | Candidate review checks the actual merge/confirm/reject branch. Quality reads filter allowed sides in SQL, including direct IDs; review checks the stored side again after locking. Calibration preview requires its own action and cost field. |

The executable HTTP inventory is `backend/tests/test_authz_s2_b2b3_entries.py`: 63 live method/path pairs plus the separately enumerated retired pool rebuild endpoint. Every live entry is exercised with a real authenticated admin lacking canonical Grants; no role implicitly grants a business action. Inventory equality fails if any of the eight owned routers gains or loses an unbound entry. This is an entry contract and refusal check, not a substitute for the semantic positive/projection tests.

## Composite and locator contract

- Overview: any allowed lookup block or substitute.read admits the request. Denied identity returns `part:null`; internal resolution reads only ID, PN, status and merge target. No description/category/brand lookup is performed for denied identity.
- Pool analysis: `analysis.read OR facts.read` admits details and their locator list. Facts-only reads do not query reference aggregates. Transactions and both old aliases share the facts projection.
- Pool management: `catalog.read OR policy.read` admits details and their locator list. Policy-only results do not query members or description. Policy fields and history still require policy.read plus data_pool_price_governance.
- Company pool: summary, sales transactions and customer cross-brand are independent. Without summary, summary calculations and purchase facts do not run; demand/benchmark/savings/metrics are null, members empty, purchase totals null.
- Each independent pool read includes the fixed necessary locator `group_id/name/version/status`. This is an explicit small information grant, not a claim of zero identity information. Other denied catalog/analysis identity details are null.
- Locator-only lists return `total:null`, `ranking_restricted:true`, `effective_sort:"group_id"`, `effective_search:"name"`. They do not count or rank hidden summary facts. Search matches only name; explicit member PN/purchase-type filtering without analysis returns 403. Default financial sort cannot order locator results.
- Existing `restricted` flags remain compatible. A denied block returns arrays empty and counts/financial values null. An allowed empty result remains distinguishable through `block_access`.

## Writes and concurrency

Domain locks precede `SysUser FOR SHARE` with a refreshed live principal. Every later access in that transaction reuses the already locked actor. No path takes actor SHARE and then upgrades to actor UPDATE. This matters for two writes by the same user: pool-member FK insertion can wait on a part writer while holding actor SHARE; the part writer must also use SHARE and complete, not request UPDATE and deadlock.

Protected domain primitives accept a transaction-owned callback/commit mode. The facade authorizes after existing domain locks, projects the response before commit and rolls back on failure. Pool policy read-back is completed before commit, so a later PDP failure cannot report a committed domain write as an uncommitted authorization error.

The normalization batch preserves its historical **per-row commit** behavior. Each row locks its part/master domain, revalidates the actor and normalize_apply, projects, then commits. An epoch change between rows leaves the first committed row intact and rejects subsequent work. Empty input still requires the action.

Alias review obtains the master advisory lock before resolving the alias and refreshes its current mapping before ordering parent locks. Candidate/merge/rename locks remain domain-first. Merge/unmerge retain their existing committed identity command followed by independent best-effort recalculation; canonical replies omit recomputation statistics and contain only a success marker or the existing generic failure marker.

## Evidence

All commands used the existing `.venv/bin/pytest`, an isolated per-process PostgreSQL test database derived from local port 32780, and the real local Cerbos binary. No production endpoint was used.

| Evidence | Result | Log |
| --- | --- | --- |
| Owned entry manifest, legacy page contract, pool ranking, financial identity rounding | 104 passed | `/tmp/itsp-authz-s2-entry-and-legacy-complete.log` |
| Legacy dashboard typed-token fixtures and existing read-only snapshot/concurrent writer tests | 22 passed | `/tmp/itsp-authz-s2-legacy-http-adapter-final.log` |
| Composite projections, independent blocks, per-row revocation, legacy pool API and batch normalization | 69 passed | `/tmp/itsp-authz-s2-composite-and-row-revision.log` |
| Independent frozen seed expectations (63 vectors), part tests and governance tests, including real alias lock wait and direct-history SQL | 89 passed | `/tmp/itsp-authz-s2-independent-seed-and-locks.log` |

These suites overlap; the counts are not a unique cumulative total. The new semantic files are `test_authz_s2_pool_access.py`, `test_authz_s2_part_access.py`, `test_authz_s2_governance_access.py`; new HTTP inventory and independent seed files are separate. Old tests were changed only where their fake role dependency no longer crossed the real typed identity adapter, or fixed authorization metadata queries needed to be distinguished from unchanged business-query budgets. Existing payload, financial identity, rollback and scope assertions remain.

Policy-only write receipts had a further independently reproduced leak: set_price_policy returned catalog member_count/creator metadata even though the same user's GET correctly hid them. The new real HTTP test failed before the fix. The facade now requests optional catalog.read in the same locked decision batch and nulls the catalog details before commit; locator and authorized policy content remain. A partially enabled composite still fails as a whole. The original row DTO is retained. Red log: `/tmp/itsp-authz-s2-policy-write-receipt-red.log`; fixed pool authorization + existing pool catalog regression: **45 passed**, `/tmp/itsp-authz-s2-policy-write-final.log`.

The other reviewer separately reported real Agent dispatch of all three part facade functions with identity-only Grants and no denied facts SQL. That evidence belongs to the Agent author's independent suite, not the author counts above.

## Root core/migration third-angle review

Reviewed `c9d4e8f2a6b1_business_authorization.py`, `seed_data/authz_s2.json`, `service.read_accesses`, `field_context`, and policy generation. The independent 63-vector expected interpreter is now permanent pytest, using explicit business conditions and expected per-resource field sets rather than evaluating the migration's own AST. It covers 7 role classes × 9 saved/legacy/override combinations, all 17 B2/B3 resources, inactive entitlement preservation, scoped sales, governance cost gating, pool governance versus purchase cost, and the legacy substitute role exception.

The migration preserves resource-key existence, including empty denial; canonical-origin false never receives inferred role grants. Existing source/profile materialization must agree before upgrade. Saved template snapshots retain their source rather than joining current templates. Downgrade locks its source tables before guards, rejects cutover/provenance/current-source drift and advances counters rather than reusing old epochs/Grant versions. Unchanged nonempty canonical bundles/Grants now receive receipts without an upgrade epoch change, fixing the author's identified empty-sales-denial rollback case.

The core reads one live actor and batch Grants, checks the exact requested resource/action set and produces a resource-paired immutable read set. The field adapter explicitly resets every data group and does not carry an old page/action permission map. The generated policies use only registered_account and explicit Grant actions/scopes/dependencies; they do not grant by old business role.

Independent re-execution: 86 passed in `/tmp/itsp-authz-s2-core-independent-rerun.log` = 63 independently authored seed vectors + 16 author migration cases + 7 author read-set cases. The latter 23 are re-execution evidence, not independently designed tests. `generate_authz_policies.py --check` verified all 23 checked-in policies. No remaining core/migration blocking finding was found in this review scope; this does not replace the stage-wide independent review or CI gate.
