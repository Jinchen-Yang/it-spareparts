import { useContextQuery } from "../contextQuery";
import { domainApi, useContextTransport } from "../domainTransport";
import { useLayoutEffect, useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../../test/antdRender";
import { api } from "../../api";
import { uploadMaintenanceCollectionEvidence } from "../../api/maintenanceOperations";
import { AuthzProvider } from "../AuthzProvider";
import { useAuthorizedQuery, type AuthorizedRequest } from "../access";
import { useAuthorizationContext, useAuthorizedOperation, contextInputKey, type ContextInput } from "../context";
import { ContextChanged, ContextProtocolError, validateAuthorizationContext, validateContextualPayload } from "../contextProtocol";
import { actionRule, ruleAllowed, ruleState, surfaceRule, vocabulary } from "../rules";
import { authzSnapshot } from "../../test/authzFixtures";
import type { AuthorizationContext } from "../contextTypes";
const { getAuthzSnapshot } = vi.hoisted(() => ({ getAuthzSnapshot: vi.fn() }));
vi.mock("../../api/authz", async original => ({ ...await original<typeof import("../../api/authz")>(), getAuthzSnapshot }));
const PROJECT = "GET /api/maintenance/projects/stable/{project_id}";
const UPDATE_RECEIPT = "PATCH /api/maintenance/return-receipts/{receipt_id}";
function snapshot(version = "v1") {
  const value = authzSnapshot({ enabled: true, read: false, export: false, scope: "none", customer_count: 0,
    reason: "", field_permissions: [] }, { mode: "unified_modules", version });
  for (const id of ["maintenance_project", "maintenance_return_receipt", "maintenance_ai_document", "maintenance_ai_ledger", "maintenance_collection_followup"]) {
    const definition = vocabulary[id];
    value.resources[id] = { migration_state: "implemented", runtime_state: "enabled", configurable: true, grant_version: 1,
      scope_kind: Object.keys(definition.scopes)[0], field_permissions: Object.keys(definition.fields),
      decisions: Object.fromEntries(Object.entries(definition.actions).map(([action, descriptor]) => [action,
        { effect: descriptor.kind === "display" ? "allow" : "requires_context", reason: null }])) };
  }
  return value;
}
function context(id = "A", resource = "maintenance_project", action = "read", kind: "project" | "object" = "project"): AuthorizationContext {
  return { schema_version: 1, catalog_version: "authz-s1-v1", authorization_epoch: 0,
    context: kind === "object" ? { kind, object_type: "return_receipt", id, authorization_revision: `revision-${id}` } :
      { kind, id, authorization_revision: `revision-${id}` },
    resources: { [resource]: { grant_version: 1, decisions: { [action]: { effect: "allow", reason: null } },
      field_permissions: Object.keys(vocabulary[resource].fields) } } };
}
const resolution = (value = context()) => ({ locator: value.context.kind === "project" ? { project_id: value.context.id } :
  value.context.kind === "collection" ? { collection_ref: value.context.id } :
    { object_type: value.context.object_type, object_id: value.context.id }, authorization_context: value });
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
const request = vi.spyOn(api, "request");
beforeEach(() => { localStorage.setItem("token", "owner"); getAuthzSnapshot.mockReset(); getAuthzSnapshot.mockResolvedValue({ data: snapshot() }); request.mockReset(); });
afterEach(cleanup);

it("display is an explicit switch; unresolved data permits only the parsing shell", () => {
  const actor = snapshot();
  const rule = surfaceRule("maintenance_project.overview");
  expect(ruleState(actor, rule)).toBe("resolve");
  expect(ruleAllowed(actor, rule)).toBe(false);
  expect(ruleAllowed(actor, rule, {}, context())).toBe(true);
  actor.resources.maintenance_project.decisions.show_overview.effect = "deny";
  expect(ruleAllowed(actor, rule, {}, context())).toBe(false);
  actor.resources.maintenance_project.decisions.show_overview.effect = "allow";
  actor.resources.maintenance_project.decisions.read.effect = "deny";
  expect(ruleState(actor, rule)).toBe("deny");
});

it("rejects unknown kinds/actions, stale grant/epoch, foreign fields and wrong locators", () => {
  const actor = snapshot();
  const valid = context();
  const expected = { entryId: PROJECT, locator: { kind: "project" as const, id: "A" } };
  expect(validateAuthorizationContext(valid, actor, expected)).toBe(valid);
  const poisons = [
    { ...valid, authorization_epoch: 3 },
    { ...valid, context: { ...valid.context, id: "B" } },
    { ...valid, context: { kind: "object", object_type: "invented", id: "A", authorization_revision: "x" } },
    { ...valid, resources: { maintenance_project: { ...valid.resources.maintenance_project, grant_version: 2 } } },
    { ...valid, resources: { maintenance_project: { ...valid.resources.maintenance_project, decisions: { update: { effect: "allow", reason: null } } } } },
    { ...valid, resources: { maintenance_project: { ...valid.resources.maintenance_project, field_permissions: ["data_customer"] } } },
  ];
  for (const value of poisons) expect(() => validateAuthorizationContext(value, actor, expected)).toThrow(ContextProtocolError);
  const changed = { ...valid, context: { ...valid.context, authorization_revision: "changed" } };
  expect(() => validateContextualPayload({ authorization_context: changed }, actor, expected, valid)).toThrow(ContextChanged);
});

function ReadProbe({ id = "A", load, frames = [] }: { id?: string; load: (capture: AuthorizedRequest) => Promise<string>; frames?: string[] }) {
  const resolved = useAuthorizationContext({ entryId: PROJECT, input: { pathParams: { project_id: id }, locator: { kind: "project", id } } });
  const query = useAuthorizedQuery({ rule: actionRule("maintenance_project", "read"), context: resolved.context,
    queryKey: [id], query: load, enabled: resolved.allowed });
  useLayoutEffect(() => { frames.push(`${id}:${query.data ?? "empty"}:${query.allowed}`); });
  return <><span>{resolved.status}</span><span>{query.data ?? "empty"}</span><button onClick={query.retry}>retry</button></>;
}
it("real Provider sends only the fixed authorization view until object allow arrives", async () => {
  const gate = deferred<any>(); request.mockReturnValue(gate.promise);
  const load = vi.fn().mockResolvedValue("business");
  render(<AuthzProvider token="owner"><ReadProbe load={load} /></AuthzProvider>);
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  expect(load).not.toHaveBeenCalled();
  expect(request.mock.calls[0][0]).toMatchObject({ method: "GET", url: "/maintenance/projects/stable/A", params: { view: "authorization" },
    headers: { Authorization: "Bearer owner" }, signal: expect.any(AbortSignal) });
  await act(async () => gate.resolve({ data: resolution() }));
  expect(await screen.findByText("business")).toBeVisible();
  expect(load).toHaveBeenCalledTimes(1);
  expect(load.mock.calls[0][0]).toMatchObject({ token: "owner", snapshotVersion: "v1", authorizationContext: context() });
});
it("A to B clears allow/data in layout phase, aborts A and ignores its delayed result", async () => {
  const gateB = deferred<any>(), oldQuery = deferred<string>(), frames: string[] = [];
  request.mockResolvedValueOnce({ data: resolution() }).mockReturnValueOnce(gateB.promise);
  const captures: AuthorizedRequest[] = [];
  const load = vi.fn((capture: AuthorizedRequest) => { captures.push(capture); return captures.length === 1 ? oldQuery.promise : Promise.resolve("B current"); });
  const view = render(<AuthzProvider token="owner"><ReadProbe load={load} frames={frames} /></AuthzProvider>);
  await waitFor(() => expect(load).toHaveBeenCalledTimes(1));
  view.rerender(<AuthzProvider token="owner"><ReadProbe id="B" load={load} frames={frames} /></AuthzProvider>);
  expect(frames.filter(f => f.startsWith("B:")).every(f => f === "B:empty:false")).toBe(true);
  expect(captures[0].signal.aborted).toBe(true);
  await act(async () => oldQuery.resolve("A stale"));
  expect(screen.queryByText("A stale")).not.toBeInTheDocument();
  await act(async () => gateB.resolve({ data: resolution(context("B")) }));
  expect(await screen.findByText("B current")).toBeVisible();
});

function WriteProbe({ input, done, errors }: { input: ContextInput; done: (value: unknown) => void; errors: unknown[] }) {
  const run = useAuthorizedOperation({ entryId: UPDATE_RECEIPT, input });
  return <button onClick={() => { void run({ commit: done }).catch(error => errors.push(error)); }}>write</button>;
}
it("a child write uses its own view and one carrier; 409 never replays or commits", async () => {
  const value = context("receipt1", "maintenance_return_receipt", "update", "object");
  request.mockResolvedValueOnce({ data: resolution(value) }).mockRejectedValueOnce({ response: { status: 409 } });
  const done = vi.fn(), errors: unknown[] = [];
  render(<AuthzProvider token="owner"><WriteProbe input={{ pathParams: { receipt_id: "receipt1" },
    body: { version: 1, reason: "correct" }, locator: { kind: "object", object_type: "return_receipt", id: "receipt1" } }} done={done} errors={errors} /></AuthzProvider>);
  await waitFor(() => expect(getAuthzSnapshot).toHaveBeenCalledTimes(1)); await act(async () => {});
  fireEvent.click(screen.getByText("write"));
  await waitFor(() => expect(errors).toHaveLength(1));
  expect(request).toHaveBeenCalledTimes(2);
  expect(request.mock.calls[0][0]).toMatchObject({ method: "PATCH", params: { view: "authorization" }, data: { version: 1, reason: "correct" } });
  expect(request.mock.calls[1][0]).toMatchObject({ method: "PATCH", data: { version: 1, reason: "correct", authorization_precondition: {
    catalog_version: value.catalog_version, authorization_epoch: 0, context: value.context,
  } } });
  expect(done).not.toHaveBeenCalled();
});
it("changing input aborts authorization and prevents execute even if the old view later allows", async () => {
  const gate = deferred<any>(), done = vi.fn(), errors: unknown[] = [];
  request.mockReturnValue(gate.promise);
  const input = { pathParams: { receipt_id: "receipt1" }, body: { version: 1, reason: "old" } };
  const view = render(<AuthzProvider token="owner"><WriteProbe input={input} done={done} errors={errors} /></AuthzProvider>);
  await act(async () => {}); fireEvent.click(screen.getByText("write"));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  const signal = request.mock.calls[0][0].signal as AbortSignal;
  view.rerender(<AuthzProvider token="owner"><WriteProbe input={{ ...input, body: { version: 1, reason: "new" } }} done={done} errors={errors} /></AuthzProvider>);
  expect(signal.aborted).toBe(true);
  await act(async () => gate.resolve({ data: resolution(context("receipt1", "maintenance_return_receipt", "update", "object")) }));
  expect(request).toHaveBeenCalledTimes(1); expect(done).not.toHaveBeenCalled();
  expect(errors).toHaveLength(1);
});
it("503 on authorization fails closed without issuing business data or retrying implicitly", async () => {
  request.mockRejectedValue({ response: { status: 503 } });
  const load = vi.fn();
  render(<AuthzProvider token="owner"><ReadProbe load={load} /></AuthzProvider>);
  expect(await screen.findByText("error")).toBeVisible();
  fireEvent.click(screen.getByText("retry"));
  expect(load).not.toHaveBeenCalled(); expect(request).toHaveBeenCalledTimes(1);
});

function TransportProbe({ id = "A", frames = [] }: { id?: string; frames?: string[] }) {
  const result = useContextQuery({ entryId: PROJECT, rule: surfaceRule("maintenance_project.overview"),
    queryKey: [id], query: async capture => (await domainApi(capture).get<{ label: string }>(`/maintenance/projects/stable/${id}`)).data });
  useLayoutEffect(() => { frames.push(`${id}:${result.data?.label ?? "empty"}`); });
  return <><span>{result.data?.label ?? "empty"}</span>{result.error ? <span>request-failed</span> : null}</>;
}
it("domain transport attaches one precondition and rejects a changed formal-read revision", async () => {
  const prior = context();
  request.mockResolvedValueOnce({ data: resolution(prior) }).mockResolvedValueOnce({ data: {
    label: "must not render", authorization_context: { ...prior, context: { ...prior.context, authorization_revision: "changed" } },
  } });
  render(<AuthzProvider token="owner"><TransportProbe /></AuthzProvider>);
  expect(await screen.findByText("request-failed")).toBeVisible();
  expect(screen.queryByText("must not render")).not.toBeInTheDocument();
  expect(request).toHaveBeenCalledTimes(2);
  expect(request.mock.calls[1][0]).toMatchObject({ headers: { Authorization: "Bearer owner" },
    params: { authorization_precondition: JSON.stringify({ catalog_version: prior.catalog_version, authorization_epoch: 0, context: prior.context }) } });
});
it("registered path locator prevents a different project's allow from issuing the formal read", async () => {
  request.mockResolvedValue({ data: resolution(context("B")) });
  render(<AuthzProvider token="owner"><TransportProbe /></AuthzProvider>);
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  await act(async () => {});
  expect(screen.queryByText("business")).not.toBeInTheDocument();
  expect(request).toHaveBeenCalledTimes(1);
});
it("domain query switches object synchronously, aborts business payload and never paints the old object", async () => {
  const oldRead = deferred<any>(), frames: string[] = [];
  request.mockImplementation(config => (config.params as { view?: string } | undefined)?.view === "authorization" ?
    Promise.resolve({ data: resolution(context(config.url!.endsWith("/A") ? "A" : "B")) }) :
    config.url!.endsWith("/A") ? oldRead.promise : Promise.resolve({ data: { label: "B payload", authorization_context: context("B") } }));
  const view = render(<AuthzProvider token="owner"><TransportProbe frames={frames} /></AuthzProvider>);
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  const oldSignal = request.mock.calls[1][0].signal as AbortSignal;
  view.rerender(<AuthzProvider token="owner"><TransportProbe id="B" frames={frames} /></AuthzProvider>);
  expect(frames.filter(frame => frame.startsWith("B:")).every(frame => frame === "B:empty")).toBe(true);
  expect(oldSignal.aborted).toBe(true);
  expect(await screen.findByText("B payload")).toBeVisible();
  await act(async () => oldRead.resolve({ data: { label: "A stale", authorization_context: context() } }));
  expect(screen.queryByText("A stale")).not.toBeInTheDocument();
});
it("explicit show denial suppresses even the authorization view despite an available read", async () => {
  const actor = snapshot(); actor.resources.maintenance_project.decisions.show_overview.effect = "deny";
  getAuthzSnapshot.mockResolvedValue({ data: actor });
  render(<AuthzProvider token="owner"><TransportProbe /></AuthzProvider>);
  await act(async () => {});
  expect(request).not.toHaveBeenCalled(); expect(screen.getByText("empty")).toBeVisible();
});

it("a partial branch cannot omit its registered action even when every supplied decision is valid", () => {
  const partial = context(); partial.resources.maintenance_project.decisions = {};
  expect(() => validateAuthorizationContext(partial, snapshot(), { entryId: PROJECT })).toThrow(ContextProtocolError);
  const omitted = { ...context(), resources: {} };
  expect(() => validateAuthorizationContext(omitted, snapshot(), { entryId: PROJECT })).toThrow(ContextProtocolError);
});
it("a formal response with unchanged carrier but denied read cannot expose its payload", async () => {
  const denied = context(); denied.resources.maintenance_project.decisions.read.effect = "deny";
  request.mockResolvedValueOnce({ data: resolution() }).mockResolvedValueOnce({ data: { label: "poisoned facts", authorization_context: denied } });
  render(<AuthzProvider token="owner"><TransportProbe /></AuthzProvider>);
  expect(await screen.findByText("request-failed")).toBeVisible();
  expect(screen.queryByText("poisoned facts")).not.toBeInTheDocument(); expect(request).toHaveBeenCalledTimes(2);
});

it("workspace admission is OR across independent blocks while its complete response contract remains mandatory", () => {
  const actor = snapshot();
  const requirements = {
    maintenance_project: "read", maintenance_project_operations: "read_workspace", maintenance_contract: "read",
    maintenance_collection: "read_snapshots", maintenance_issue: "read_workspace", maintenance_expense: "read_workspace",
  };
  const entryId = "GET /api/maintenance/projects/stable/{project_id}/workspace";
  const value: AuthorizationContext = { ...context(), resources: {} };
  for (const [id, action] of Object.entries(requirements)) {
    const admitted = id === "maintenance_collection";
    actor.resources[id] = { migration_state: "implemented", runtime_state: "enabled", configurable: true,
      grant_version: admitted ? 1 : 0, scope_kind: admitted ? Object.keys(vocabulary[id].scopes)[0] : "none", field_permissions: [],
      decisions: Object.fromEntries(Object.keys(vocabulary[id].actions).map(key => [key,
        { effect: admitted && key === action ? "requires_context" : "deny", reason: null }])) };
    value.resources[id] = { grant_version: admitted ? 1 : null, field_permissions: [],
      decisions: { [action]: { effect: admitted ? "allow" : "deny", reason: null } } };
  }
  const payload = { collection_snapshots: { rows: [{ collection_id: "own-collection" }] }, authorization_context: value };
  expect(validateContextualPayload(payload, actor, { entryId }, value)).toBe(payload);
  const incomplete = { ...value, resources: { ...value.resources } };
  delete incomplete.resources.maintenance_contract;
  expect(() => validateAuthorizationContext(incomplete, actor, { entryId })).toThrow(ContextProtocolError);
  const allDenied = { ...value, resources: { ...value.resources, maintenance_collection: {
    ...value.resources.maintenance_collection, decisions: { read_snapshots: { effect: "deny" as const, reason: null } },
  } } };
  expect(() => validateContextualPayload({ ...payload, authorization_context: allDenied }, actor, { entryId }, value)).toThrow("没有此对象");
});

function RefreshProbe({ done }: { done: (success: boolean) => void }) {
  const query = useContextQuery({ entryId: PROJECT, rule: surfaceRule("maintenance_project.overview"), queryKey: ["A"],
    query: async capture => (await domainApi(capture).get<{ label: string }>("/maintenance/projects/stable/A")).data });
  return <><span>{query.data?.label ?? "empty"}</span><button onClick={() => { void query.retry().then(done); }}>refresh-barrier</button></>;
}
it("a readback barrier resolves only after its current formal read and returns false on failed refresh", async () => {
  const refreshed = deferred<any>(), done = vi.fn();
  request.mockResolvedValueOnce({ data: resolution() }).mockResolvedValueOnce({ data: { label: "before", authorization_context: context() } })
    .mockResolvedValueOnce({ data: resolution() }).mockReturnValueOnce(refreshed.promise);
  render(<AuthzProvider token="owner"><RefreshProbe done={done} /></AuthzProvider>);
  expect(await screen.findByText("before")).toBeVisible();
  fireEvent.click(screen.getByText("refresh-barrier"));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(4));
  expect(screen.queryByText("before")).not.toBeInTheDocument(); expect(done).not.toHaveBeenCalled();
  await act(async () => refreshed.resolve({ data: { label: "after", authorization_context: context() } }));
  expect(await screen.findByText("after")).toBeVisible(); expect(done).toHaveBeenLastCalledWith(true);
  request.mockRejectedValueOnce({ response: { status: 403 } });
  fireEvent.click(screen.getByText("refresh-barrier"));
  await waitFor(() => expect(done).toHaveBeenLastCalledWith(false));
  expect(screen.queryByText("after")).not.toBeInTheDocument(); expect(request).toHaveBeenCalledTimes(5);
});

function MixedWorkbookProbe() {
  const capture = useContextTransport("project-A", true);
  const [result, setResult] = useState("");
  return <><button disabled={!capture.isCurrent()} onClick={() => {
    void domainApi(capture).get("/maintenance/projects/stable/A/master-workbook.xlsx?sheets=03_%E5%A4%87%E4%BB%B6%E8%AE%A2%E5%8D%95", { responseType: "blob" })
      .then(() => setResult("downloaded"), () => setResult("rejected"));
  }}>mixed-workbook</button><span>{result}</span></>;
}
it.each(["allow", "deny"] as const)("mixed canonical and legacy workbook alternatives require an authorization view (%s)", async effect => {
  const actor = snapshot();
  for (const id of ["maintenance_managed_workbook", "maintenance_general_workbook"]) {
    actor.resources[id] = { migration_state: "implemented", runtime_state: id === "maintenance_managed_workbook" ? "legacy" : "enabled",
      configurable: true, grant_version: 1, scope_kind: Object.keys(vocabulary[id].scopes)[0], field_permissions: Object.keys(vocabulary[id].fields),
      decisions: Object.fromEntries(Object.keys(vocabulary[id].actions).map(action => [action,
        { effect: id === "maintenance_managed_workbook" ? "deny" : "requires_context", reason: null }])) };
  }
  Object.assign(actor.resources.maintenance_managed_workbook, { configurable: false, grant_version: 0, scope_kind: "none", field_permissions: [] });
  getAuthzSnapshot.mockResolvedValue({ data: actor });
  const proof = context("A", "maintenance_general_workbook", "download");
  proof.resources.maintenance_general_workbook.decisions.download.effect = effect;
  const pending = deferred<any>();
  request.mockReturnValueOnce(pending.promise).mockResolvedValueOnce({ data: new Blob(["file"]) });
  render(<AuthzProvider token="owner"><MixedWorkbookProbe /></AuthzProvider>);
  await waitFor(() => expect(screen.getByText("mixed-workbook")).toBeEnabled());
  fireEvent.click(screen.getByText("mixed-workbook"));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  expect(request.mock.calls[0][0].params).toMatchObject({ view: "authorization", sheets: "03_备件订单" });
  expect(screen.queryByText("downloaded")).not.toBeInTheDocument();
  await act(async () => pending.resolve({ data: resolution(proof) }));
  expect(await screen.findByText(effect === "allow" ? "downloaded" : "rejected")).toBeVisible();
  expect(request).toHaveBeenCalledTimes(effect === "allow" ? 2 : 1);
  if (effect === "allow") expect(request.mock.calls[1][0].params).toMatchObject({ sheets: "03_备件订单", authorization_precondition: expect.any(String) });
});


function EvidenceUploadProbe({ file, done, errors }: { file: File; done: (value: unknown) => void; errors: unknown[] }) {
  const capture = useContextTransport(contextInputKey({ pathParams: { milestone_id: "milestone-A" }, body: file }));
  return <button disabled={!capture.isCurrent()} onClick={() => {
    void uploadMaintenanceCollectionEvidence("milestone-A", file, capture).then(done, error => errors.push(error));
  }}>upload-evidence</button>;
}
function evidenceContext(): AuthorizationContext {
  return { ...context("milestone-A", "maintenance_collection_followup", "evidence_upload", "object"),
    context: { kind: "object", object_type: "collection_milestone", id: "milestone-A", authorization_revision: "actual-file-digest-revision" } };
}
it.each([200, 409])("evidence multipart uses the same actual File and one server precondition (%s)", async status => {
  const file = new File(["actual evidence bytes"], "receipt.pdf", { type: "application/pdf", lastModified: 1 });
  const proof = evidenceContext(), done = vi.fn(), errors: unknown[] = [];
  request.mockResolvedValueOnce({ data: resolution(proof) });
  if (status === 200) request.mockResolvedValueOnce({ data: { evidence_id: "file-A" } });
  else request.mockRejectedValueOnce({ response: { status: 409 } });
  render(<AuthzProvider token="owner"><EvidenceUploadProbe file={file} done={done} errors={errors} /></AuthzProvider>);
  await waitFor(() => expect(screen.getByText("upload-evidence")).toBeEnabled());
  fireEvent.click(screen.getByText("upload-evidence"));
  await waitFor(() => expect(status === 200 ? done.mock.calls.length : errors.length).toBe(1));
  expect(request).toHaveBeenCalledTimes(2);
  const viewBody = request.mock.calls[0][0].data as FormData;
  const executeBody = request.mock.calls[1][0].data as FormData;
  expect(viewBody.get("file")).toBe(file); expect([...viewBody.keys()]).toEqual(["file"]);
  expect(executeBody.get("file")).toBe(file);
  expect([...executeBody.keys()]).toEqual(["file", "authorization_precondition"]);
  expect(JSON.parse(String(executeBody.get("authorization_precondition")))).toEqual({
    catalog_version: proof.catalog_version, authorization_epoch: proof.authorization_epoch, context: proof.context,
  });
  expect(request.mock.calls[0][0]).toMatchObject({ method: "POST", params: { view: "authorization" },
    url: "/maintenance/collection-milestones/milestone-A/evidence", headers: { Authorization: "Bearer owner" } });
  if (status === 409) expect(done).not.toHaveBeenCalled();
});
it("replacing evidence with a same-name same-size File aborts its pending authorization and never uploads", async () => {
  const old = new File(["old"], "receipt.pdf", { type: "application/pdf", lastModified: 1 });
  const next = new File(["new"], "receipt.pdf", { type: "application/pdf", lastModified: 1 });
  const gate = deferred<any>(), done = vi.fn(), errors: unknown[] = [];
  request.mockReturnValueOnce(gate.promise);
  const view = render(<AuthzProvider token="owner"><EvidenceUploadProbe file={old} done={done} errors={errors} /></AuthzProvider>);
  await waitFor(() => expect(screen.getByText("upload-evidence")).toBeEnabled());
  fireEvent.click(screen.getByText("upload-evidence"));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  const signal = request.mock.calls[0][0].signal as AbortSignal;
  view.rerender(<AuthzProvider token="owner"><EvidenceUploadProbe file={next} done={done} errors={errors} /></AuthzProvider>);
  expect(signal.aborted).toBe(true);
  await act(async () => gate.resolve({ data: resolution(evidenceContext()) }));
  expect(request).toHaveBeenCalledTimes(1); expect(done).not.toHaveBeenCalled(); expect(errors).toHaveLength(1);
});
