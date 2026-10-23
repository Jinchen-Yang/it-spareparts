import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import api from "../../api";
import JobsPanel from "./JobsPanel";
const snapshot = { schema_version: 1, resources: { part_gallery: { migration_state: "implemented", runtime_state: "enabled", decisions: Object.fromEntries(["job.read", "job.cancel", "job.retry", "enrichment.bundle.read"].map(a => [a, { effect: "allow" }])), field_permissions: ["gallery"] } } };
vi.mock("../../authz/AuthzProvider", async importOriginal => ({ ...await importOriginal<object>(), useAuthz: () => ({ status: "ready", snapshot }) }));
const original = api.defaults.adapter;
beforeEach(() => { localStorage.setItem("token", "job-editor"); });
afterEach(() => { cleanup(); api.defaults.adapter = original; });
const job = { job_id: "job", kind: "enrichment.bundle", status: "running", part_id: 42, resource_id: "42", input_revision: 7, attempt: 2, retry_count: 1, revision: 9, error_code: null, result: null, created_at: "2026-01-01T00:00:00Z" };
it("取消只使用已显示的任务版本、尝试及重试次数", async () => {
  const writes: unknown[] = [];
  api.defaults.adapter = async config => { if (config.method === "post") writes.push(JSON.parse(config.data)); return { data: { data: config.method === "post" ? { ...job, status: "cancelled", revision: 10 } : [job], meta: {} }, config, headers: {}, status: 200, statusText: "OK" }; };
  render(<JobsPanel partId={42} />); fireEvent.click(await screen.findByRole("button", { name: "取消任务" }));
  await waitFor(() => expect(writes).toHaveLength(1)); expect(writes[0]).toEqual({ expected_revision: 9, expected_attempt: 2, expected_retry_count: 1 });
});
it("每次 ZIP 下载重新签名；撤权时清除旧链接", async () => {
  let signs = 0;
  api.defaults.adapter = async config => {
    let data: unknown = [{ ...job, status: "succeeded" }];
    if (config.url?.endsWith("/bundle-downloads")) {
      if (++signs > 1) throw Object.assign(new Error("denied"), { response: { status: 403, data: { error: { code: "auth.forbidden", message: "授权已撤回" } } } });
      data = { expires_in: 900, archives: [{ number: 1, filename: "images.zip", url: "https://storage.invalid/zip?token=fresh", image_count: 2, size_bytes: 100 }] };
    }
    return { data: { data, meta: {} }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<JobsPanel partId={42} />); fireEvent.click(await screen.findByRole("button", { name: "获取当前下载链接" }));
  await screen.findByRole("link", { name: /images.zip/ }); fireEvent.click(screen.getByRole("button", { name: "获取当前下载链接" }));
  await screen.findByText("授权已撤回"); expect(screen.queryByRole("link", { name: /images.zip/ })).not.toBeInTheDocument(); expect(signs).toBe(2);
});
