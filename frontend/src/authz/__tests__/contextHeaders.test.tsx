import { AxiosHeaders, type AxiosRequestConfig } from "axios";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import { api } from "../../api";
import { uploadReturnReceiptImport } from "../../api/maintenanceReturnReceiptImports";
import { AuthzProvider } from "../AuthzProvider";
import { contextInputKey, useAuthorizedOperation } from "../context";
import { domainApi, useContextTransport } from "../domainTransport";
import { vocabulary } from "../rules";
import { authzSnapshot } from "../../test/authzFixtures";
import type { AuthorizationContext } from "../contextTypes";
const { getAuthzSnapshot } = vi.hoisted(() => ({ getAuthzSnapshot: vi.fn() }));
vi.mock("../../api/authz", async original => ({ ...await original<typeof import("../../api/authz")>(), getAuthzSnapshot }));
const path = "/maintenance/doc-imports/return-receipts/jobs";
const entry = `POST /api${path}`;
const request = vi.spyOn(api, "request");
function sentHeaders(config: AxiosRequestConfig) {
  const headers = new AxiosHeaders();
  for (const [name, value] of Object.entries(config.headers ?? {})) {
    if (value !== undefined) headers.set(name, value);
  }
  return headers;
}
const proof: AuthorizationContext = { schema_version: 1, catalog_version: "authz-s1-v1", authorization_epoch: 0,
  context: { kind: "object", object_type: "operation_input", id: "file-digest", authorization_revision: "file-and-key-revision" },
  resources: { maintenance_return_import: { grant_version: 1, field_permissions: [], decisions: { submit: { effect: "allow", reason: null } } } } };
const resolution = { locator: { object_type: "operation_input", object_id: "file-digest" }, authorization_context: proof };
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
function actor() {
  const value = authzSnapshot({ enabled: true, read: false, export: false, scope: "none", customer_count: 0,
    reason: "", field_permissions: [] }, { mode: "unified_modules", version: "v1" });
  value.resources.maintenance_return_import = { migration_state: "implemented", runtime_state: "enabled", configurable: true,
    grant_version: 1, scope_kind: "all_projects", field_permissions: [],
    decisions: Object.fromEntries(Object.keys(vocabulary.maintenance_return_import.actions).map(action =>
      [action, { effect: action === "submit" ? "requires_context" : "deny", reason: null }])) };
  return value;
}
beforeEach(() => { localStorage.setItem("token", "owner"); getAuthzSnapshot.mockReset(); getAuthzSnapshot.mockResolvedValue({ data: actor() }); request.mockReset(); });
afterEach(cleanup);
function UploadProbe({ file, headers, done, errors }: {
  file: File; headers?: AxiosRequestConfig["headers"]; done: (value: unknown) => void; errors: unknown[];
}) {
  const capture = useContextTransport(contextInputKey({ body: file, headers }));
  return <button disabled={!capture.isCurrent()} onClick={() => {
    const body = new FormData(); body.append("file", file);
    void (headers ? domainApi(capture).post(path, body, { headers, auth: { username: "stale", password: "stale" } })
      : uploadReturnReceiptImport(file, "same-business-key", capture)).then(done, error => errors.push(error));
  }}>upload</button>;
}
it.each([202, 409])("the actual import helper preserves its idempotency header and File in both stages (%s)", async status => {
  const file = new File(["real workbook bytes"], "return.xlsx"), done = vi.fn(), errors: unknown[] = [];
  request.mockResolvedValueOnce({ data: resolution });
  if (status === 202) request.mockResolvedValueOnce({ data: { batch_id: "one-job", status: "queued" } });
  else request.mockRejectedValueOnce({ response: { status: 409 } });
  render(<AuthzProvider token="owner"><UploadProbe file={file} done={done} errors={errors} /></AuthzProvider>);
  const button = await screen.findByText("upload"); await waitFor(() => expect(button).toBeEnabled()); fireEvent.click(button);
  await waitFor(() => expect(done.mock.calls.length + errors.length).toBe(1));
  expect(request).toHaveBeenCalledTimes(2);
  for (const [config] of request.mock.calls) {
    expect(sentHeaders(config).get("Idempotency-Key")).toBe("same-business-key");
    expect(sentHeaders(config).get("Authorization")).toBe("Bearer owner");
    expect((config.data as FormData).get("file")).toBe(file);
  }
  expect([...(request.mock.calls[0][0].data as FormData).keys()]).toEqual(["file"]);
  expect([...(request.mock.calls[1][0].data as FormData).keys()]).toEqual(["file", "authorization_precondition"]);
  if (status === 409) expect(done).not.toHaveBeenCalled();
});
it.each(["record", "axios"])("captured identity overrides caller authorization case-insensitively (%s)", async kind => {
  const file = new File(["workbook"], "return.xlsx"), done = vi.fn(), errors: unknown[] = [];
  const headers = { authorization: "Bearer stale-owner", "idempotency-key": "caller-command-key", "Content-Type": false as const };
  request.mockResolvedValueOnce({ data: resolution }).mockResolvedValueOnce({ data: {} });
  render(<AuthzProvider token="owner"><UploadProbe file={file} headers={kind === "axios" ? new AxiosHeaders(headers) : headers} done={done} errors={errors} /></AuthzProvider>);
  const button = await screen.findByText("upload"); await waitFor(() => expect(button).toBeEnabled()); fireEvent.click(button);
  await waitFor(() => expect(done).toHaveBeenCalledTimes(1));
  for (const [config] of request.mock.calls) {
    const sent = sentHeaders(config);
    expect(sent.get("Idempotency-Key")).toBe("caller-command-key");
    expect(sent.get("Authorization")).toBe("Bearer owner");
    expect(Object.keys(config.headers!).filter(key => key.toLowerCase() === "authorization")).toEqual(["Authorization"]);
    expect(sent.get("Content-Type")).toBe(false);
    expect(config.auth).toBeUndefined();
  }
  expect(headers.authorization).toBe("Bearer stale-owner");
});
it.each(["mutate", "rerender"])("changing an idempotency key during authorization never issues a formal command (%s)", async mode => {
  const file = new File(["workbook"], "return.xlsx"), done = vi.fn(), errors: unknown[] = [];
  const headers = { "Idempotency-Key": "old-command-key" }, pending = deferred<any>();
  request.mockReturnValue(pending.promise);
  const view = render(<AuthzProvider token="owner"><UploadProbe file={file} headers={headers} done={done} errors={errors} /></AuthzProvider>);
  const button = await screen.findByText("upload"); await waitFor(() => expect(button).toBeEnabled()); fireEvent.click(button);
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  const signal = request.mock.calls[0][0].signal;
  if (mode === "mutate") headers["Idempotency-Key"] = "new-command-key";
  else view.rerender(<AuthzProvider token="owner"><UploadProbe file={file} headers={{ "Idempotency-Key": "new-command-key" }} done={done} errors={errors} /></AuthzProvider>);
  if (mode === "rerender") expect(signal?.aborted).toBe(true);
  await act(async () => pending.resolve({ data: resolution }));
  expect(request).toHaveBeenCalledTimes(1); expect(done).not.toHaveBeenCalled(); expect(errors).toHaveLength(1);
});
function OperationProbe({ file, done }: { file: File; done: (value: unknown) => void }) {
  const body = new FormData(); body.append("file", file);
  const run = useAuthorizedOperation({ entryId: entry, input: { body, headers: { "Idempotency-Key": "operation-command-key", Authorization: "Bearer stale" } } });
  return <button onClick={() => void run({ commit: done })}>explicit operation</button>;
}
it("explicit operations use the same generic captured-header transport", async () => {
  const done = vi.fn(); request.mockResolvedValueOnce({ data: resolution }).mockResolvedValueOnce({ data: {} });
  render(<AuthzProvider token="owner"><OperationProbe file={new File(["file"], "return.xlsx")} done={done} /></AuthzProvider>);
  await act(async () => {}); fireEvent.click(screen.getByText("explicit operation"));
  await waitFor(() => expect(done).toHaveBeenCalledTimes(1));
  expect(request).toHaveBeenCalledTimes(2);
  for (const [config] of request.mock.calls) {
    expect(sentHeaders(config).get("Idempotency-Key")).toBe("operation-command-key");
    expect(sentHeaders(config).get("Authorization")).toBe("Bearer owner");
  }
});

it("legacy transport keeps the same business header while still pinning the captured owner", async () => {
  const legacy = actor();
  Object.assign(legacy.resources.maintenance_return_import, { runtime_state: "legacy", grant_version: 0, configurable: false, scope_kind: "none" });
  for (const decision of Object.values(legacy.resources.maintenance_return_import.decisions)) decision.effect = "deny";
  getAuthzSnapshot.mockResolvedValue({ data: legacy });
  const done = vi.fn(), errors: unknown[] = [], file = new File(["file"], "return.xlsx");
  request.mockResolvedValue({ data: { batch_id: "legacy-one-job" } });
  function LegacyProbe() {
    const capture = useContextTransport("legacy-upload", true);
    return <button disabled={!capture.isCurrent()} onClick={() => void uploadReturnReceiptImport(file, "legacy-command-key", capture).then(done, error => errors.push(error))}>legacy upload</button>;
  }
  render(<AuthzProvider token="owner"><LegacyProbe /></AuthzProvider>);
  const button = await screen.findByText("legacy upload"); await waitFor(() => expect(button).toBeEnabled()); fireEvent.click(button);
  await waitFor(() => expect(done).toHaveBeenCalledTimes(1));
  expect(errors).toEqual([]); expect(request).toHaveBeenCalledTimes(1);
  expect(sentHeaders(request.mock.calls[0][0]).get("Idempotency-Key")).toBe("legacy-command-key");
  expect(sentHeaders(request.mock.calls[0][0]).get("Authorization")).toBe("Bearer owner");
});
