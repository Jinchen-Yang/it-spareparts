import { legacyAuthorizationState, prepareLegacyPageFixture } from "../../../test/legacyAuthorization";
vi.mock("../../../authz/AuthzProvider", async importOriginal => ({
  ...await importOriginal<typeof import("../../../authz/AuthzProvider")>(),
  useAuthz: () => { prepareLegacyPageFixture({ page_master_data: true }); return legacyAuthorizationState(); },
}));
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../../../test/antdRender";
import { api } from "../../../api";
import InlinePartEditModal from "../InlinePartEditModal";

const originalAdapter = api.defaults.adapter;
const baseline = {
  part_id: 42,
  pn_std: "PN-42",
  identity: { part_id: 42, pn_std: "PN-42", status: "active", merged_into_id: null },
  fields: {
    description: null, brand: "Old Brand", category_major: "硬盘", category_minor: null,
    machine_or_part: null, unit: null,
  },
  specs: {},
  version_mode: "field_snapshot",
};

function response(config: any, data: unknown) {
  return { data, status: 200, statusText: "OK", headers: {}, config };
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("token", "editor-token");
  localStorage.setItem("role", "admin");
});

afterEach(() => {
  cleanup();
  api.defaults.adapter = originalAdapter;
});

it("内联编辑冲突后可采用当前基线并保留草稿，重新提交只包含人工修改", async () => {
  const patches: any[] = [];
  let loads = 0;
  const latest = { ...baseline, fields: {
    ...baseline.fields, description: "另一人描述", category_major: "内存", category_minor: "DDR4",
  } };
  api.defaults.adapter = async (config) => {
    if (config.method === "patch") {
      patches.push(JSON.parse(config.data));
      if (patches.length > 1) return response(config, {
        id: 42, pn_std: "PN-42", updated: ["description"], locked_fields: ["description"],
      });
      throw Object.assign(new Error("conflict"), { config, response: {
        status: 409,
        data: { detail: { code: "field_conflict", message: "描述已变化，请核对", conflicts: [{
          field: "description", baseline: null, current: "另一人描述", proposed: "人工新描述",
        }] } },
      } });
    }
    if (config.url === "/parts/overview") {
      return response(config, { part: { id: 42, pn_std: "PN-42", ...baseline.fields } });
    }
    if (config.url === "/parts/master/42/edit-state") {
      return response(config, ++loads === 1 ? baseline : latest);
    }
    return response(config, { categories: [] });
  };
  const onClose = vi.fn();
  const onSaved = vi.fn();
  render(<InlinePartEditModal open canEdit contextKey="42" pn="PN-42"
    onClose={onClose} onSaved={onSaved} />);

  await waitFor(() => expect(screen.getByLabelText("描述")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("描述"), { target: { value: "人工新描述" } });
  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));

  await waitFor(() => expect(patches).toHaveLength(1));
  expect(patches[0]).toEqual({
    part_id: 42, pn_std: "PN-42", base_snapshot: baseline, description: "人工新描述",
  });
  expect(await screen.findByText("描述已变化，请核对")).toBeInTheDocument();
  expect(screen.getByText("描述：打开时「未填」；当前「另一人描述」；本次「人工新描述」")).toBeInTheDocument();
  expect(screen.getByLabelText("描述")).toHaveValue("人工新描述");
  expect(onClose).not.toHaveBeenCalled();
  expect(onSaved).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "以当前值为新基线并保留我的草稿" }));
  await waitFor(() => expect(loads).toBe(2));
  await waitFor(() => expect(screen.queryByText("描述已变化，请核对")).toBeNull());
  expect(screen.getByLabelText("描述")).toHaveValue("人工新描述");
  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
  await waitFor(() => expect(patches).toHaveLength(2));
  expect(patches[1]).toEqual({
    part_id: 42, pn_std: "PN-42", base_snapshot: latest, description: "人工新描述",
  });
  await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  await act(async () => {});
});

it("编辑加载均绑定原会话，账号切换后的旧加载不能借新 token 或填入表单", async () => {
  const requests: { url?: string; auth: unknown }[] = [];
  api.defaults.adapter = async (config) => {
    requests.push({ url: config.url, auth: config.headers.Authorization });
    if (config.url === "/parts/overview") {
      return response(config, { part: { id: 42, pn_std: "PN-42", ...baseline.fields } });
    }
    return response(config, { categories: [] });
  };
  render(<InlinePartEditModal open canEdit contextKey="42" pn="PN-42"
    onClose={vi.fn()} onSaved={vi.fn()} />);
  localStorage.setItem("token", "another-account");
  await act(async () => {});

  expect(requests).toEqual([
    { url: "/parts/overview", auth: "Bearer editor-token" },
    { url: "/parts/master/categories", auth: "Bearer editor-token" },
  ]);
  expect(screen.getByLabelText("描述")).toBeDisabled();
});

it("详情已知的稳定 ID 在打开编辑前被合并时，拒绝编辑而不转到目标型号", async () => {
  const requested: string[] = [];
  api.defaults.adapter = async (config) => {
    requested.push(config.url!);
    if (config.url === "/parts/overview") return response(config, {
      part: { id: 43, pn_std: "PN-43", ...baseline.fields, description: "合并目标" },
    });
    if (config.url === "/parts/master/42/edit-state") return response(config, {
      ...baseline,
      identity: { ...baseline.identity, status: "merged", merged_into_id: 43 },
    });
    if (config.url === "/parts/master/43/edit-state") return response(config, {
      ...baseline, part_id: 43, pn_std: "PN-43",
      identity: { ...baseline.identity, part_id: 43, pn_std: "PN-43" },
      fields: { ...baseline.fields, description: "合并目标" },
    });
    return response(config, { categories: [] });
  };
  render(<InlinePartEditModal open canEdit contextKey="42" partId={42} pn="PN-42"
    onClose={vi.fn()} onSaved={vi.fn()} />);

  expect(await screen.findByText("型号身份已变化，请关闭后重新打开编辑")).toBeInTheDocument();
  expect(requested).toContain("/parts/master/42/edit-state");
  expect(requested).not.toContain("/parts/overview");
  expect(requested).not.toContain("/parts/master/43/edit-state");
  expect(screen.getByRole("button", { name: /保\s*存/ })).toBeDisabled();
});

it.each([true, false])("打开编辑前已改名时拒绝旧 PN 草稿（携稳定 ID：%s）", async (withId) => {
  const requested: string[] = [];
  api.defaults.adapter = async (config) => {
    requested.push(config.url!);
    if (config.url === "/parts/overview") return response(config, {
      part: { id: 43, pn_std: "PN-RENAMED", ...baseline.fields },
    });
    if (config.url?.endsWith("/edit-state")) return response(config, {
      ...baseline, pn_std: "PN-RENAMED", identity: { ...baseline.identity, pn_std: "PN-RENAMED" },
    });
    return response(config, { categories: [] });
  };
  render(<InlinePartEditModal open canEdit contextKey="42" partId={withId ? 42 : undefined} pn="PN-42"
    onClose={vi.fn()} onSaved={vi.fn()} />);

  expect(await screen.findByText("型号身份已变化，请关闭后重新打开编辑")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /保\s*存/ })).toBeDisabled();
  expect(requested).not.toContain("/parts/master/43/edit-state");
});

it("同 PN 更换页面上下文后重新加载，旧上下文的迟到基线不覆盖新表单", async () => {
  let release!: (value: any) => void;
  const delayed = new Promise<any>((resolve) => { release = resolve; });
  let loads = 0;
  api.defaults.adapter = async (config) => {
    if (config.url === "/parts/master/42/edit-state") {
      loads += 1;
      return loads === 1 ? delayed : response(config, {
        ...baseline, fields: { ...baseline.fields, description: "新上下文描述" },
      });
    }
    return response(config, { categories: [] });
  };
  const props = { open: true, canEdit: true, partId: 42, pn: "PN-42", onClose: vi.fn(), onSaved: vi.fn() };
  const view = render(<InlinePartEditModal {...props} contextKey="first" />);
  await waitFor(() => expect(loads).toBe(1));
  view.rerender(<InlinePartEditModal {...props} contextKey="second" />);
  await screen.findByDisplayValue("新上下文描述");
  await act(async () => { release(response({}, baseline)); await delayed; });
  expect(screen.getByLabelText("描述")).toHaveValue("新上下文描述");
});

it("旧编辑加载迟到 401 时保留新账号 token，不向新账号显示旧加载结果", async () => {
  let rejectLoad!: (reason: unknown) => void;
  let loadConfig: any;
  const delayed = new Promise<any>((_, reject) => { rejectLoad = reject; });
  api.defaults.adapter = async (config) => {
    if (config.url === "/parts/master/42/edit-state") { loadConfig = config; return delayed; }
    return response(config, { categories: [] });
  };
  render(<InlinePartEditModal open canEdit contextKey="42" partId={42} pn="PN-42"
    onClose={vi.fn()} onSaved={vi.fn()} />);
  await waitFor(() => expect(loadConfig).toBeDefined());
  expect(loadConfig.headers.Authorization).toBe("Bearer editor-token");
  localStorage.setItem("token", "new-account");
  await act(async () => {
    rejectLoad(Object.assign(new Error("unauthorized"), { config: loadConfig, response: { status: 401 } }));
  });
  expect(localStorage.getItem("token")).toBe("new-account");
  expect(screen.queryByText("unauthorized")).toBeNull();
  expect(screen.getByLabelText("描述")).toBeDisabled();
});

it("刷新冲突基线时两级品类一起保留，不把我的二级品类接到他人的一级品类", async () => {
  let loads = 0;
  const patches: any[] = [];
  const latest = { ...baseline, fields: { ...baseline.fields, category_major: "内存", category_minor: "DDR4" } };
  api.defaults.adapter = async (config) => {
    if (config.method === "patch") {
      patches.push(JSON.parse(config.data));
      if (patches.length === 1) throw Object.assign(new Error("conflict"), { config, response: {
        status: 409, data: { detail: { code: "field_conflict", message: "品类已变化", conflicts: [] } },
      } });
      return response(config, { id: 42, pn_std: "PN-42", updated: ["category_major", "category_minor"] });
    }
    if (config.url === "/parts/master/42/edit-state") return response(config, ++loads === 1 ? baseline : latest);
    return response(config, { categories: [{ id: 1, name: "硬盘", children: [{ id: 2, name: "SAS" }] }] });
  };
  render(<InlinePartEditModal open canEdit contextKey="42" partId={42} pn="PN-42"
    onClose={vi.fn()} onSaved={vi.fn()} />);
  await waitFor(() => expect(screen.getByLabelText("二级品类")).toBeEnabled());
  fireEvent.mouseDown(screen.getByLabelText("二级品类"));
  await screen.findByRole("option", { name: "SAS" });
  const option = screen.getAllByText("SAS").find((node) => node.closest(".ant-select-item-option"));
  fireEvent.click(option!.closest(".ant-select-item-option")!);
  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
  fireEvent.click(await screen.findByRole("button", { name: "以当前值为新基线并保留我的草稿" }));
  await waitFor(() => expect(screen.queryByText("品类已变化")).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
  await waitFor(() => expect(patches).toHaveLength(2));
  expect(patches[1]).toEqual({
    part_id: 42, pn_std: "PN-42", base_snapshot: latest, category_major: "硬盘", category_minor: "SAS",
  });
});

it.each(["改名", "合并"])("冲突后刷新基线遇到%s时保留草稿并拒绝切换身份", async (change) => {
  let loads = 0;
  const latest = change === "改名"
    ? { ...baseline, pn_std: "RENAMED", identity: { ...baseline.identity, pn_std: "RENAMED" } }
    : { ...baseline, identity: { ...baseline.identity, status: "merged", merged_into_id: 43 } };
  api.defaults.adapter = async (config) => {
    if (config.method === "patch") throw Object.assign(new Error("conflict"), { config, response: {
      status: 409, data: { detail: { code: "field_conflict", message: "描述已变化", conflicts: [] } },
    } });
    if (config.url === "/parts/master/42/edit-state") return response(config, ++loads === 1 ? baseline : latest);
    return response(config, { categories: [] });
  };
  render(<InlinePartEditModal open canEdit contextKey="42" partId={42} pn="PN-42"
    onClose={vi.fn()} onSaved={vi.fn()} />);
  await waitFor(() => expect(screen.getByLabelText("描述")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("描述"), { target: { value: "保留的草稿" } });
  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
  fireEvent.click(await screen.findByRole("button", { name: "以当前值为新基线并保留我的草稿" }));
  expect(await screen.findByText("型号身份已变化，草稿已保留，请关闭后重新打开编辑")).toBeInTheDocument();
  expect(screen.getByLabelText("描述")).toHaveValue("保留的草稿");
  expect(screen.getByLabelText("PN")).toHaveValue("PN-42");
});

it.each(["账号", "上下文"])("冲突基线慢加载期间切换%s，迟到结果不会覆盖草稿或新上下文", async (change) => {
  let loads = 0;
  let rebaseConfig: any;
  let release!: (value: any) => void;
  const delayed = new Promise<any>((resolve) => { release = resolve; });
  api.defaults.adapter = async (config) => {
    if (config.method === "patch") throw Object.assign(new Error("conflict"), { config, response: {
      status: 409, data: { detail: { code: "field_conflict", message: "描述已变化", conflicts: [] } },
    } });
    if (config.url === "/parts/master/42/edit-state") {
      ++loads;
      if (loads === 2) { rebaseConfig = config; return delayed; }
      return response(config, loads === 1 ? baseline : {
        ...baseline, fields: { ...baseline.fields, description: "新上下文描述" },
      });
    }
    return response(config, { categories: [] });
  };
  const props = { open: true, canEdit: true, partId: 42, pn: "PN-42", onClose: vi.fn(), onSaved: vi.fn() };
  const view = render(<InlinePartEditModal {...props} contextKey="first" />);
  await waitFor(() => expect(screen.getByLabelText("描述")).toBeEnabled());
  fireEvent.change(screen.getByLabelText("描述"), { target: { value: "原账号草稿" } });
  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
  fireEvent.click(await screen.findByRole("button", { name: "以当前值为新基线并保留我的草稿" }));
  await waitFor(() => expect(rebaseConfig).toBeDefined());
  expect(rebaseConfig.headers.Authorization).toBe("Bearer editor-token");
  expect(screen.getByLabelText("描述")).toBeDisabled();
  expect(screen.getByRole("button", { name: /保\s*存/ })).toBeDisabled();
  if (change === "账号") localStorage.setItem("token", "new-account");
  else {
    view.rerender(<InlinePartEditModal {...props} contextKey="next" />);
    await screen.findByDisplayValue("新上下文描述");
  }
  await act(async () => {
    release(response(rebaseConfig, { ...baseline, fields: { ...baseline.fields, description: "迟到基线" } }));
    await delayed;
  });
  expect(screen.getByLabelText("描述")).toHaveValue(change === "账号" ? "原账号草稿" : "新上下文描述");
  expect(localStorage.getItem("token")).toBe(change === "账号" ? "new-account" : "editor-token");
});

// These cases exercise legacy page behavior; admission has an explicit fixture.
beforeEach(() => prepareLegacyPageFixture({ page_master_data: true }));
