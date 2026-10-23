import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "../../test/antdRender";
import api from "../../api";
import EnrichmentPanel from "./EnrichmentPanel";
import { part } from "./fixtures";
const resource = (actions: string[], fields: string[]) => ({ migration_state: "implemented", runtime_state: "enabled", decisions: Object.fromEntries(actions.map(a => [a, { effect: "allow" }])), field_permissions: fields });
const snapshot = { schema_version: 1, resources: {
  part_master: resource(["read", "enrichment.research", "enrichment.research.text", "create_candidate", "read_candidates", "job.read"], ["candidates"]),
  part_content: resource(["read"], ["content.good.title", "content.bad.title"]),
  part_gallery: resource(["gallery.read", "enrichment.bundle", "enrichment.bundle.read", "job.read"], ["gallery"]),
} };
vi.mock("../../authz/AuthzProvider", async importOriginal => ({ ...await importOriginal<object>(), useAuthz: () => ({ status: "ready", snapshot }) }));
const original = api.defaults.adapter;
beforeEach(() => { localStorage.setItem("token", "enrichment-editor"); });
afterEach(() => { cleanup(); api.defaults.adapter = original; });
const capabilities = { media: true, research: true, recognition: true, bundle: true };
const job = { job_id: "job", kind: "enrichment.research", status: "pending", part_id: 42, resource_id: "42", input_revision: 7, attempt: 0, retry_count: 0, revision: 1, error_code: null, result: null, created_at: "2026-01-01T00:00:00Z" };

it("读取与核对不启动研究；确认时提交已显示的资料和两种文案版本", async () => {
  const writes: unknown[] = [];
  api.defaults.adapter = async config => {
    let data: unknown = [];
    if (config.url?.endsWith("/parts/42")) data = part;
    if (config.url?.includes("/content/")) data = { part_id: 42, identity_revision: 2, revision: config.url.endsWith("good") ? 4 : 8, title: "current" };
    if (config.method === "post") { writes.push(JSON.parse(config.data)); data = job; }
    return { data: { data, meta: {} }, config, headers: {}, status: 200, statusText: "OK" };
  };
  render(<EnrichmentPanel partId={42} capabilities={capabilities} />);
  fireEvent.click(screen.getByRole("checkbox", { name: "资料与好坏件文案" }));
  fireEvent.click(screen.getByRole("button", { name: "核对研究范围" }));
  const dialog = await screen.findByRole("dialog"); expect(writes).toHaveLength(0);
  fireEvent.click(within(dialog).getByRole("button", { name: "开始研究" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0]).toEqual({ expected_identity_revision: 2, expected_profile_revision: 7, expected_content_revisions: { good: 4, bad: 8 }, capabilities: ["text"], max_searches: 2 });
});

it("好件打包只选择通用图，禁用的服务不会启动工作", async () => {
  const writes: unknown[] = [];
  api.defaults.adapter = async config => {
    let data: unknown = [];
    if (config.url?.endsWith("/gallery")) data = { group_id: "g", part_id: 42, revision: 11, primary_entry_id: "common", ordered_entry_ids: ["common", "defect"], entries: ["common", "defect"].map(purpose => ({ entry_id: purpose, purpose, size_bytes: 100, source: { filename: purpose }, status: "gallery" })) };
    if (config.method === "post") { writes.push(JSON.parse(config.data)); data = { ...job, kind: "enrichment.bundle" }; }
    return { data: { data, meta: { identity_revision: 2, next_cursor: null } }, config, headers: {}, status: 200, statusText: "OK" };
  };
  const rendered = render(<EnrichmentPanel partId={42} capabilities={{ ...capabilities, bundle: false }} />);
  expect(screen.getByRole("button", { name: "选择打包图片" })).toBeDisabled(); expect(writes).toHaveLength(0);
  rendered.rerender(<EnrichmentPanel partId={42} capabilities={capabilities} />);
  fireEvent.click(screen.getByRole("button", { name: "选择打包图片" }));
  const dialog = await screen.findByRole("dialog"); expect(writes).toHaveLength(0);
  expect(within(dialog).queryByText("defect")).not.toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "开始打包" })); await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0]).toEqual({ expected_identity_revision: 2, expected_gallery_revision: 11, grade: "good", entry_ids: ["common"], watermark: false });
});
