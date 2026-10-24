import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import api from "../../api";
import MigrationWorkspace from "./MigrationWorkspace";
let grantEvidence = true;
const snapshot = { schema_version: 1, resources: { part_master: { migration_state: "implemented", runtime_state: "enabled", decisions: Object.fromEntries(["read_legacy", "materialize_migration"].map(a => [a, { effect: "allow" }])), get field_permissions() { return grantEvidence ? ["legacy_evidence"] : []; } } } };
vi.mock("../../authz/AuthzProvider", async original => ({ ...await original<object>(), useAuthz: () => ({ status: "ready", snapshot }) }));
const original = api.defaults.adapter;
const frozen = { namespace: "shop", snapshot_id: "frozen-id", source_schema: "old-schema", revision: 1, verification_scope: "isolated_copy", created_at: "2026-01-01T00:00:00Z" };
const row = { source_record_id: 1, namespace: "shop", kind: "manual_research", source_id: "old-request", source_sha256: "a".repeat(64), state: "recovery_required", revision: 1, reason_code: "migration.fenced_work_reconcile", target_ref: { history: { part_id: 42 } } };
beforeEach(() => { localStorage.setItem("token", "migration-operator"); grantEvidence = true; });
afterEach(() => { cleanup(); api.defaults.adapter = original; });

it("仅读取冻结副本；恢复响应丢失时锁定决定并重用原请求号", async () => {
  const writes: { body: unknown; key: unknown }[] = [];
  api.defaults.adapter = async config => {
    let data: unknown;
    if (config.method === "post") {
      writes.push({ body: JSON.parse(config.data), key: config.headers.get("Idempotency-Key") });
      if (writes.length === 1) throw new Error("response lost");
      data = { ...row, state: "historical_terminal", revision: 2 };
    } else if (config.url?.endsWith("/snapshots")) data = [frozen];
    else if (config.url?.endsWith("/records")) data = [row];
    else data = { record_count: 1, pending: 0, live_complete: false, cutover_ready: false };
    return { data: { data, meta: { next_cursor: null } }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<MigrationWorkspace />);
  await waitFor(() => expect(screen.getByRole("combobox", { name: "选择冻结快照" })).toBeInTheDocument());
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "选择冻结快照" }));
  fireEvent.click(await screen.findByText("shop · frozen-id"));
  fireEvent.click(await screen.findByRole("button", { name: "核对恢复决定" }));
  expect(writes).toHaveLength(0);
  fireEvent.change(screen.getByLabelText("核对证据引用"), { target: { value: "isolated-review:1" } });
  fireEvent.change(screen.getByLabelText("恢复决定理由"), { target: { value: "已核对旧外呼并保留历史" } });
  fireEvent.click(screen.getByRole("button", { name: "确认并记录恢复决定" }));
  await screen.findByText("结果尚未确认。保留原决定与请求号，再次核对将恢复原请求。");
  expect(screen.getByLabelText("恢复决定理由")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "核对原请求结果" }));
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes[1]).toEqual(writes[0]);
  expect(writes[0].body).toMatchObject({ expected_revision: 1, source_sha256: row.source_sha256, decision: "retire_fenced", evidence_ref: "isolated-review:1" });
});

it("缺少历史证据字段权限时不读取快照，也不开放恢复", async () => {
  grantEvidence = false;
  const calls: string[] = [];
  api.defaults.adapter = async config => { calls.push(config.url || ""); throw new Error("unexpected request"); };
  render(<MigrationWorkspace />);
  expect(await screen.findByText("迁移核对需要明确的历史证据读取权限。")).toBeInTheDocument();
  expect(calls).toEqual([]);
});

it("原始证据仅在明确打开时读取，保留来源指纹且不发起写入", async () => {
  const calls: string[] = [];
  api.defaults.adapter = async config => {
    calls.push(config.url || "");
    expect(config.method).toBe("get");
    const data = config.url?.endsWith("/snapshots") ? [frozen] : config.url?.endsWith("/records") ? [row] : config.url?.endsWith("/evidence") ? { source_sha256: row.source_sha256, payload: { status: "running", original_actor: "historic-uuid" } } : { record_count: 1 };
    return { data: { data, meta: { next_cursor: null } }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<MigrationWorkspace />);
  fireEvent.mouseDown(await screen.findByRole("combobox", { name: "选择冻结快照" }));
  fireEvent.click(await screen.findByText("shop · frozen-id"));
  const button = await screen.findByRole("button", { name: "查看原始证据" });
  expect(calls.some(path => path.endsWith("/evidence"))).toBe(false);
  fireEvent.click(button);
  expect(await screen.findByText(/historic-uuid/)).toBeInTheDocument();
  expect(calls.filter(path => path.endsWith("/evidence"))).toHaveLength(1);
});
