import { expect, it } from "vitest";
import golden from "../../../../docs/reviews/authz-rewrite-20261023/s4-import-wire-golden.json";
import { batchDetail, jobList, jobResponse } from "../importProtocol";
const example = (id: string) => structuredClone(golden.cases.find(item => item.id === id)!.response);
it("consumes frozen report-only/status-only, partial-job and empty-list DTOs", () => {
  expect(batchDetail(example("batch_status_without_diagnostics")).report).toBeNull();
  expect(batchDetail(example("report_only_known_batch")).metadata).toBeNull();
  expect(jobResponse(example("mixed_job_partial_projection")).job.total_files).toBeNull();
  expect(jobList(example("allowed_empty_job_list")).items).toEqual([]);
});
it("rejects partial-job hidden counts, ordinal, free text and unknown error codes", () => {
  const value = example("mixed_job_partial_projection") as any;
  for (const mutate of [
    (v: any) => { v.job.total_files = 99; }, (v: any) => { v.job.items[0].input_index = 0; },
    (v: any) => { v.job.note = "hidden source"; }, (v: any) => { v.job.items[0].code = "RAW_SQL"; },
    (v: any) => { v.job.items[0].source_domain = "sales"; },
  ]) { const poison = structuredClone(value); mutate(poison); expect(() => jobResponse(poison)).toThrow(); }
});
it("rejects a report without this source's complete fields and denied-block payload", () => {
  const report = example("report_only_known_batch") as any;
  report.authorization_context.resources[`import_${report.source_domain}`].field_permissions = [];
  expect(() => batchDetail(report)).toThrow();
  const status = example("batch_status_without_diagnostics") as any; status.report = { amount: 55 };
  expect(() => batchDetail(status)).toThrow();
});
