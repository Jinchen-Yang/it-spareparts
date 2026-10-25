import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../test/antdRender";
import { api } from "../../api";
import MasterDataPage from "../MasterDataPage";

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

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

const second = {
  ...baseline, part_id: 43, pn_std: "PN-43",
  identity: { ...baseline.identity, part_id: 43, pn_std: "PN-43" },
  fields: { ...baseline.fields, description: "第二型号描述" },
};

const searchResult = { total: 2, items: [baseline, second].map((state) => ({
  id: state.part_id, pn_std: state.pn_std, ...state.fields, needs_review: false,
})) };

async function openPart(pn: string) {
  const row = await screen.findByRole("row", { name: new RegExp(pn) });
  fireEvent.click(within(row).getByRole("button", { name: /编\s*辑/ }));
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

it("主数据库编辑冲突后保留品牌清空草稿，以当前基线重提时不覆盖他人描述", async () => {
  const patches: any[] = [];
  let loads = 0;
  const latest = { ...baseline, fields: { ...baseline.fields, brand: "Other Brand", description: "他人新描述" } };
  api.defaults.adapter = async (config) => {
    if (config.method === "patch") {
      patches.push(JSON.parse(config.data));
      if (patches.length > 1) return response(config, {
        id: 42, pn_std: "PN-42", updated: ["brand"], locked_fields: ["brand"],
      });
      throw Object.assign(new Error("conflict"), { config, response: {
        status: 409,
        data: { detail: { code: "field_conflict", message: "品牌已变化，请核对", conflicts: [{
          field: "brand", baseline: "Old Brand", current: "Other Brand", proposed: null,
        }] } },
      } });
    }
    if (config.url === "/parts/search") return response(config, { total: 1, items: [{
      id: 42, pn_std: "PN-42", ...baseline.fields, needs_review: false,
    }] });
    if (config.url === "/parts/overview") {
      return response(config, { part: { id: 42, pn_std: "PN-42", ...baseline.fields } });
    }
    if (config.url === "/parts/master/42/edit-state") {
      return response(config, ++loads === 1 ? baseline : latest);
    }
    return response(config, { categories: [] });
  };
  render(<MemoryRouter><MasterDataPage /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /编\s*辑/ }));
  const dialog = await screen.findByRole("dialog", { name: /编辑 PN.*PN-42/ });
  fireEvent.change(within(dialog).getByDisplayValue("Old Brand"), { target: { value: "" } });
  fireEvent.click(within(dialog).getByRole("button", { name: /保\s*存/ }));

  await waitFor(() => expect(patches).toHaveLength(1));
  expect(patches[0]).toEqual({
    part_id: 42, pn_std: "PN-42", base_snapshot: baseline, brand: null,
  });
  expect(await within(dialog).findByText("品牌已变化，请核对")).toBeInTheDocument();
  expect(within(dialog).getByText("品牌：打开时「Old Brand」；当前「Other Brand」；本次「未填」")).toBeInTheDocument();
  expect(within(dialog).getByPlaceholderText("如 Seagate / HPE")).toHaveValue("");
  fireEvent.click(within(dialog).getByRole("button", { name: "以当前值为新基线并保留我的草稿" }));
  await waitFor(() => expect(loads).toBe(2));
  await within(dialog).findByDisplayValue("他人新描述");
  expect(within(dialog).getByLabelText("品牌")).toHaveValue("");
  fireEvent.click(within(dialog).getByRole("button", { name: /保\s*存/ }));
  await waitFor(() => expect(patches).toHaveLength(2));
  expect(patches[1]).toEqual({
    part_id: 42, pn_std: "PN-42", base_snapshot: latest, brand: null,
  });
});

it("原始类型未填时可主动选择备件，提交类型差异并保留 null 基线", async () => {
  const patches: any[] = [];
  api.defaults.adapter = async (config) => {
    if (config.method === "patch") {
      patches.push(JSON.parse(config.data));
      return response(config, { id: 42, pn_std: "PN-42", updated: ["machine_or_part"] });
    }
    if (config.url === "/parts/search") return response(config, searchResult);
    if (config.url === "/parts/master/42/edit-state") return response(config, baseline);
    return response(config, { categories: [] });
  };
  render(<MemoryRouter><MasterDataPage /></MemoryRouter>);
  await openPart("PN-42");
  const dialog = await screen.findByRole("dialog", { name: /编辑 PN.*PN-42/ });
  await within(dialog).findByDisplayValue("Old Brand");
  fireEvent.mouseDown(within(dialog).getByLabelText("类型"));
  await screen.findByRole("option", { name: "备件" });
  const option = screen.getAllByText("备件")
    .find((node) => node.closest(".ant-select-item-option"));
  fireEvent.click(option!.closest(".ant-select-item-option")!);
  fireEvent.click(within(dialog).getByRole("button", { name: /保\s*存/ }));

  await waitFor(() => expect(patches).toHaveLength(1));
  expect(patches[0]).toEqual({
    part_id: 42, pn_std: "PN-42", base_snapshot: baseline, machine_or_part: "备件",
  });
  expect(patches[0].base_snapshot.fields.machine_or_part).toBeNull();
});

it("取消慢加载后打开另一型号，旧加载不会覆盖新表单", async () => {
  const slow = deferred<any>();
  api.defaults.adapter = async (config) => {
    if (config.url === "/parts/search") return response(config, searchResult);
    if (config.url === "/parts/master/42/edit-state" || config.url === "/parts/overview") {
      return slow.promise;
    }
    if (config.url === "/parts/master/43/edit-state") return response(config, second);
    return response(config, { categories: [] });
  };
  render(<MemoryRouter><MasterDataPage /></MemoryRouter>);
  await openPart("PN-42");
  const loadingDialog = await screen.findByRole("dialog", { name: /编辑 PN.*PN-42/ });
  expect(within(loadingDialog).getByRole("button", { name: /保\s*存/ })).toBeDisabled();
  fireEvent.click(within(loadingDialog).getByRole("button", { name: /取\s*消/ }));
  await openPart("PN-43");
  const nextDialog = await screen.findByRole("dialog", { name: /编辑 PN.*PN-43/ });
  await within(nextDialog).findByDisplayValue("第二型号描述");

  await act(async () => {
    slow.resolve(response({}, { ...baseline, part: { id: 42, pn_std: "PN-42", ...baseline.fields } }));
    await slow.promise;
  });
  expect(screen.getByRole("dialog", { name: /编辑 PN.*PN-43/ })).toBeInTheDocument();
  expect(within(nextDialog).getByLabelText("描述")).toHaveValue("第二型号描述");
});

it("旧型号保存迟到时不会关闭后来打开的编辑会话或触发旧搜索", async () => {
  const save = deferred<any>();
  let searches = 0;
  let patchConfig: any;
  api.defaults.adapter = async (config) => {
    if (config.method === "patch") { patchConfig = config; return save.promise; }
    if (config.url === "/parts/search") { ++searches; return response(config, searchResult); }
    if (config.url === "/parts/master/42/edit-state") return response(config, baseline);
    if (config.url === "/parts/master/43/edit-state") return response(config, second);
    return response(config, { categories: [] });
  };
  render(<MemoryRouter><MasterDataPage /></MemoryRouter>);
  await openPart("PN-42");
  const firstDialog = await screen.findByRole("dialog", { name: /编辑 PN.*PN-42/ });
  await within(firstDialog).findByDisplayValue("Old Brand");
  fireEvent.change(within(firstDialog).getByLabelText("描述"), { target: { value: "第一型号草稿" } });
  fireEvent.click(within(firstDialog).getByRole("button", { name: /保\s*存/ }));
  await waitFor(() => expect(patchConfig).toBeDefined());
  expect(patchConfig.headers.Authorization).toBe("Bearer editor-token");
  fireEvent.click(within(firstDialog).getByRole("button", { name: /取\s*消/ }));
  await openPart("PN-43");
  const nextDialog = await screen.findByRole("dialog", { name: /编辑 PN.*PN-43/ });
  await within(nextDialog).findByDisplayValue("第二型号描述");
  fireEvent.change(within(nextDialog).getByLabelText("描述"), { target: { value: "第二型号草稿" } });
  await act(async () => {
    save.resolve(response(patchConfig, { id: 42, pn_std: "PN-42", updated: ["description"] }));
    await save.promise;
  });
  expect(within(nextDialog).getByLabelText("描述")).toHaveValue("第二型号草稿");
  expect(screen.getByRole("dialog", { name: /编辑 PN.*PN-43/ })).toBeInTheDocument();
  expect(searches).toBe(1);
});

it("没有实际修改时，空值展示和默认类型不会写入", async () => {
  let patches = 0;
  api.defaults.adapter = async (config) => {
    if (config.method === "patch") ++patches;
    if (config.url === "/parts/search") return response(config, searchResult);
    if (config.url === "/parts/master/42/edit-state") return response(config, baseline);
    return response(config, { categories: [] });
  };
  render(<MemoryRouter><MasterDataPage /></MemoryRouter>);
  await openPart("PN-42");
  const dialog = await screen.findByRole("dialog", { name: /编辑 PN.*PN-42/ });
  await within(dialog).findByDisplayValue("Old Brand");
  fireEvent.click(within(dialog).getByRole("button", { name: /保\s*存/ }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: /编辑 PN.*PN-42/ })).toBeNull());
  expect(patches).toBe(0);
});

it("旧账号保存的迟到 401 不会退出新账号或覆盖当前草稿", async () => {
  let rejectSave!: (reason: unknown) => void;
  let patchConfig: any;
  const delayed = new Promise<any>((_, reject) => { rejectSave = reject; });
  api.defaults.adapter = async (config) => {
    if (config.method === "patch") { patchConfig = config; return delayed; }
    if (config.url === "/parts/search") return response(config, searchResult);
    if (config.url === "/parts/master/42/edit-state") return response(config, baseline);
    return response(config, { categories: [] });
  };
  render(<MemoryRouter><MasterDataPage /></MemoryRouter>);
  await openPart("PN-42");
  const dialog = await screen.findByRole("dialog", { name: /编辑 PN.*PN-42/ });
  await within(dialog).findByDisplayValue("Old Brand");
  fireEvent.change(within(dialog).getByLabelText("描述"), { target: { value: "原账号草稿" } });
  fireEvent.click(within(dialog).getByRole("button", { name: /保\s*存/ }));
  await waitFor(() => expect(patchConfig).toBeDefined());
  expect(patchConfig.headers.Authorization).toBe("Bearer editor-token");
  localStorage.setItem("token", "new-account");
  await act(async () => {
    rejectSave(Object.assign(new Error("unauthorized"), { config: patchConfig, response: { status: 401 } }));
  });
  expect(localStorage.getItem("token")).toBe("new-account");
  expect(within(dialog).getByLabelText("描述")).toHaveValue("原账号草稿");
  expect(screen.queryByText("保存失败，请重试")).toBeNull();
});

it("搜索 A 慢、B 快时，A 的迟到列表不会覆盖 B", async () => {
  const slow = deferred<any>();
  let slowConfig: any;
  api.defaults.adapter = async (config) => {
    if (config.url === "/parts/search") {
      if (config.params.q === "A") { slowConfig = config; return slow.promise; }
      if (config.params.q === "B") return response(config, { total: 1, items: [searchResult.items[1]] });
      return response(config, { total: 0, items: [] });
    }
    return response(config, { categories: [] });
  };
  render(<MemoryRouter><MasterDataPage /></MemoryRouter>);
  const input = screen.getByPlaceholderText("搜 PN / 描述 / 品牌（留空浏览全部）");
  fireEvent.change(input, { target: { value: "A" } });
  fireEvent.keyDown(input, { key: "Enter", code: "Enter", keyCode: 13, charCode: 13 });
  fireEvent.keyUp(input, { key: "Enter", code: "Enter", keyCode: 13, charCode: 13 });
  await waitFor(() => expect(slowConfig).toBeDefined());
  fireEvent.change(input, { target: { value: "B" } });
  fireEvent.keyDown(input, { key: "Enter", code: "Enter", keyCode: 13, charCode: 13 });
  fireEvent.keyUp(input, { key: "Enter", code: "Enter", keyCode: 13, charCode: 13 });
  await screen.findByRole("row", { name: /PN-43/ });
  await act(async () => {
    slow.resolve(response(slowConfig, { total: 1, items: [searchResult.items[0]] }));
    await slow.promise;
  });
  expect(screen.getByRole("row", { name: /PN-43/ })).toBeInTheDocument();
  expect(screen.queryByRole("row", { name: /PN-42/ })).toBeNull();
});

it("初始类别和列表 GET 绑定原账号，换 token 后迟到结果不填入页面", async () => {
  const requests: { url?: string; auth: unknown }[] = [];
  api.defaults.adapter = async (config) => {
    requests.push({ url: config.url, auth: config.headers.Authorization });
    if (config.url === "/parts/search") return response(config, searchResult);
    return response(config, { categories: [] });
  };
  render(<MemoryRouter><MasterDataPage /></MemoryRouter>);
  localStorage.setItem("token", "new-account");
  await act(async () => {});
  expect(requests).toEqual([
    { url: "/parts/master/categories", auth: "Bearer editor-token" },
    { url: "/parts/search", auth: "Bearer editor-token" },
  ]);
  expect(screen.queryByRole("row", { name: /PN-42/ })).toBeNull();
  expect(screen.queryByRole("row", { name: /PN-43/ })).toBeNull();
});

it("保存期间不能用一键规范化修改待保存的草稿", async () => {
  const delayed = deferred<any>();
  let patchConfig: any;
  api.defaults.adapter = async (config) => {
    if (config.method === "patch") { patchConfig = config; return delayed.promise; }
    if (config.url === "/parts/search") return response(config, searchResult);
    if (config.url === "/parts/master/42/edit-state") return response(config, baseline);
    if (config.url === "/parts/master/suggest") return response(config, {
      suggestion: { canonical_description: "建议描述", review_status: "AUTO_OK" },
    });
    return response(config, { categories: [] });
  };
  render(<MemoryRouter><MasterDataPage /></MemoryRouter>);
  await openPart("PN-42");
  const dialog = await screen.findByRole("dialog", { name: /编辑 PN.*PN-42/ });
  await within(dialog).findByDisplayValue("Old Brand");
  const input = within(dialog).getByLabelText("描述");
  fireEvent.change(input, { target: { value: "保存中的草稿" } });
  fireEvent.blur(input);
  const normalize = await within(dialog).findByRole("button", { name: "一键规范化" });
  fireEvent.click(within(dialog).getByRole("button", { name: /保\s*存/ }));
  await waitFor(() => expect(patchConfig).toBeDefined());
  try {
    expect(normalize).toBeDisabled();
    fireEvent.click(normalize);
    expect(input).toHaveValue("保存中的草稿");
  } finally {
    await act(async () => {
      delayed.resolve(response(patchConfig, { id: 42, pn_std: "PN-42", updated: ["description"] }));
      await delayed.promise;
    });
  }
});

it.each(["改名", "合并"])("主数据库冲突后重取基线遇到%s时不改身份并保留草稿", async (change) => {
  let loads = 0;
  const latest = change === "改名"
    ? { ...baseline, pn_std: "RENAMED", identity: { ...baseline.identity, pn_std: "RENAMED" } }
    : { ...baseline, identity: { ...baseline.identity, status: "merged", merged_into_id: 43 } };
  api.defaults.adapter = async (config) => {
    if (config.method === "patch") throw Object.assign(new Error("conflict"), { config, response: {
      status: 409, data: { detail: { code: "field_conflict", message: "品牌已变化", conflicts: [] } },
    } });
    if (config.url === "/parts/search") return response(config, searchResult);
    if (config.url === "/parts/master/42/edit-state") return response(config, ++loads === 1 ? baseline : latest);
    return response(config, { categories: [] });
  };
  render(<MemoryRouter><MasterDataPage /></MemoryRouter>);
  await openPart("PN-42");
  const dialog = await screen.findByRole("dialog", { name: /编辑 PN.*PN-42/ });
  await within(dialog).findByDisplayValue("Old Brand");
  fireEvent.change(within(dialog).getByLabelText("品牌"), { target: { value: "我的品牌草稿" } });
  fireEvent.click(within(dialog).getByRole("button", { name: /保\s*存/ }));
  fireEvent.click(await within(dialog).findByRole("button", { name: "以当前值为新基线并保留我的草稿" }));
  expect(await within(dialog).findByText("型号身份已变化，草稿已保留，请关闭后重新打开编辑")).toBeInTheDocument();
  expect(within(dialog).getByLabelText("PN")).toHaveValue("PN-42");
  expect(within(dialog).getByLabelText("品牌")).toHaveValue("我的品牌草稿");
});

it.each(["账号", "型号"])("主数据库冲突基线慢加载期间切换%s，迟到结果不覆盖当前编辑", async (change) => {
  let loads = 0;
  let rebaseConfig: any;
  const slow = deferred<any>();
  api.defaults.adapter = async (config) => {
    if (config.method === "patch") throw Object.assign(new Error("conflict"), { config, response: {
      status: 409, data: { detail: { code: "field_conflict", message: "描述已变化", conflicts: [] } },
    } });
    if (config.url === "/parts/search") return response(config, searchResult);
    if (config.url === "/parts/master/42/edit-state") {
      if (++loads === 2) { rebaseConfig = config; return slow.promise; }
      return response(config, baseline);
    }
    if (config.url === "/parts/master/43/edit-state") return response(config, second);
    return response(config, { categories: [] });
  };
  render(<MemoryRouter><MasterDataPage /></MemoryRouter>);
  await openPart("PN-42");
  const dialog = await screen.findByRole("dialog", { name: /编辑 PN.*PN-42/ });
  await within(dialog).findByDisplayValue("Old Brand");
  fireEvent.change(within(dialog).getByLabelText("描述"), { target: { value: "原账号草稿" } });
  fireEvent.click(within(dialog).getByRole("button", { name: /保\s*存/ }));
  fireEvent.click(await within(dialog).findByRole("button", { name: "以当前值为新基线并保留我的草稿" }));
  await waitFor(() => expect(rebaseConfig).toBeDefined());
  expect(rebaseConfig.headers.Authorization).toBe("Bearer editor-token");
  expect(within(dialog).getByLabelText("描述")).toBeDisabled();
  expect(within(dialog).getByRole("button", { name: /保\s*存/ })).toBeDisabled();
  if (change === "账号") localStorage.setItem("token", "new-account");
  else {
    fireEvent.click(within(dialog).getByRole("button", { name: /取\s*消/ }));
    await openPart("PN-43");
    await screen.findByDisplayValue("第二型号描述");
  }
  await act(async () => {
    slow.resolve(response(rebaseConfig, { ...baseline, fields: { ...baseline.fields, description: "迟到基线" } }));
    await slow.promise;
  });
  expect(screen.getByLabelText("描述")).toHaveValue(change === "账号" ? "原账号草稿" : "第二型号描述");
  expect(localStorage.getItem("token")).toBe(change === "账号" ? "new-account" : "editor-token");
});

it("新建 PN 标准必填标记及空白错误关联输入，保存启用条件不变", async () => {
  api.defaults.adapter = async (config) => response(config, { categories: [], items: [], total: 0 });
  render(<MemoryRouter><MasterDataPage /></MemoryRouter>);
  fireEvent.click(screen.getByRole("button", { name: "新建 PN" }));
  const dialog = await screen.findByRole("dialog", { name: "新建 PN" });
  const input = within(dialog).getByLabelText("PN（产品唯一标识）");
  expect(input).toHaveAttribute("aria-required", "true");
  fireEvent.change(input, { target: { value: "   " } });
  fireEvent.blur(input);
  expect(input).toHaveAttribute("aria-invalid", "true");
  await waitFor(() => expect(document.getElementById(input.getAttribute("aria-describedby")!)).toHaveTextContent("请输入 PN"));
  expect(within(dialog).getByRole("button", { name: /保\s*存/ })).toBeDisabled();
  fireEvent.change(input, { target: { value: "SYNTH-PN" } });
  expect(input).not.toHaveAttribute("aria-invalid");
  expect(within(dialog).getByRole("button", { name: /保\s*存/ })).toBeEnabled();
});
