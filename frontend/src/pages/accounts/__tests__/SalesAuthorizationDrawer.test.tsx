import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import SalesAuthorizationDrawer from "../SalesAuthorizationDrawer";
import type { SalesAuthorization } from "../../../api/authz";
const mocks = vi.hoisted(() => ({
  get: vi.fn(), put: vi.fn(), search: vi.fn(), refresh: vi.fn(),
  authz: { status: "ready", snapshot: { username: "operator", can_manage: true, sales_analysis: { enabled: true } } },
}));
vi.mock("../../../api/authz", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../../api/authz")>(),
  getSalesAuthorization: mocks.get, updateSalesAuthorization: mocks.put, searchAuthzCustomers: mocks.search,
}));
vi.mock("../../../authz/AuthzProvider", () => ({ useAuthz: () => ({ ...mocks.authz, refresh: mocks.refresh }) }));
const initial: SalesAuthorization = { username: "target", version: 3, actions: ["read"], scope: "assigned_customers",
  field_permissions: ["data_customer"], customer_ids: [53], customers: [{ id: 53, name: "未在首屏的已选客户" }] };
const onClose = vi.fn(); const onSaved = vi.fn();
const mount = () => render(<SalesAuthorizationDrawer username="target" onClose={onClose} onSaved={onSaved} />);
async function loaded() { expect(await screen.findByRole("button", { name: "负责客户" })).toBeInTheDocument(); }
async function openPicker() { fireEvent.click(screen.getByRole("button", { name: "负责客户" })); return await screen.findByRole("dialog", { name: "负责客户筛选" }); }
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); localStorage.setItem("token", "owner");
  mocks.authz.status = "ready"; mocks.authz.snapshot.can_manage = true; mocks.authz.snapshot.username = "operator";
  mocks.get.mockResolvedValue({ data: structuredClone(initial) }); mocks.put.mockResolvedValue({ data: initial });
  mocks.search.mockImplementation((q: string) => Promise.resolve({ data: { items: q ? [{ id: 2, name: "搜索客户乙" }] : [{ id: 1, name: "客户甲" }] } }));
});
afterEach(cleanup);
it("客户搜索、勾选、面板重置取消不写授权，保留候选页外的全量已选客户", async () => {
  mount(); await loaded(); let panel = await openPicker();
  expect(within(panel).getByLabelText("已选列表")).toHaveTextContent("未在首屏的已选客户");
  fireEvent.click(await within(panel).findByRole("checkbox", { name: "客户甲（ID 1）" }));
  fireEvent.change(within(panel).getByRole("textbox", { name: "搜索负责客户" }), { target: { value: "乙" } });
  fireEvent.click(await within(panel).findByRole("checkbox", { name: "搜索客户乙（ID 2）" }));
  expect(within(panel).getByLabelText("已选列表")).toHaveTextContent("已选 3 个");
  fireEvent.click(within(panel).getByRole("button", { name: "重置" }));
  expect(within(panel).getByRole("textbox", { name: "搜索负责客户" })).toHaveValue("");
  fireEvent.keyDown(within(panel).getByRole("textbox"), { key: "Escape" });
  expect(screen.getByLabelText("已确认负责客户")).toHaveTextContent("未在首屏的已选客户");
  expect(mocks.put).not.toHaveBeenCalled();
  panel = await openPicker();
  fireEvent.click(await within(panel).findByRole("checkbox", { name: "客户甲（ID 1）" }));
  fireEvent.click(within(panel).getByRole("button", { name: "确定" }));
  expect(screen.getByLabelText("已确认负责客户")).toHaveTextContent("未在首屏的已选客户");
  expect(screen.getByLabelText("已确认负责客户")).toHaveTextContent("客户甲");
  expect(mocks.put).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("checkbox", { name: "导出销售分析" }));
  fireEvent.click(screen.getByRole("button", { name: "保存授权" }));
  await waitFor(() => expect(mocks.put).toHaveBeenCalledWith("target", {
    expected_version: 3, actions: ["read", "export"], scope: "assigned_customers", field_permissions: ["data_customer"], customer_ids: [53, 1],
  }, "owner"));
  expect(onSaved).toHaveBeenCalledOnce();
  expect(await screen.findByText(/该账号原登录已失效/)).toBeInTheDocument();
});
it("重置须确定且最终保存后才清客户，关闭查看同时取消导出", async () => {
  mocks.get.mockResolvedValue({ data: { ...initial, actions: ["read", "export"] } });
  mount(); await loaded(); const panel = await openPicker();
  fireEvent.click(within(panel).getByRole("button", { name: "重置" }));
  fireEvent.click(within(panel).getByRole("button", { name: "确定" }));
  expect(mocks.put).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("checkbox", { name: "查看销售分析" }));
  expect(screen.getByRole("checkbox", { name: "导出销售分析" })).toBeDisabled();
  expect(screen.getByRole("checkbox", { name: "导出销售分析" })).not.toBeChecked();
  fireEvent.click(screen.getByRole("button", { name: "保存授权" }));
  await waitFor(() => expect(mocks.put).toHaveBeenCalledWith("target", expect.objectContaining({ actions: [], customer_ids: [] }), "owner"));
});
it("409 保留草稿并要求重新加载，不自动覆盖新版本", async () => {
  mocks.put.mockRejectedValue({ response: { status: 409 } }); mount(); await loaded();
  fireEvent.click(screen.getByRole("checkbox", { name: "导出销售分析" }));
  fireEvent.click(screen.getByRole("button", { name: "保存授权" }));
  expect(await screen.findByText(/其他管理员已更新/)).toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "导出销售分析" })).toBeChecked();
  expect(mocks.put).toHaveBeenCalledTimes(1); expect(onSaved).not.toHaveBeenCalled();
});
it.each(["利润与毛利率", "销售对账信息"])("%s 缺成本阻止提交，不默默扩大字段授权", async label => {
  mount(); await loaded(); fireEvent.click(screen.getByRole("checkbox", { name: label }));
  fireEvent.click(screen.getByRole("button", { name: "保存授权" }));
  expect(await screen.findByText(/需要同时勾选成本/)).toBeInTheDocument(); expect(mocks.put).not.toHaveBeenCalled();
});
it("静默更换 token 后旧管理草稿无法提交", async () => {
  mount(); await loaded(); localStorage.setItem("token", "new-user");
  fireEvent.click(screen.getByRole("button", { name: "保存授权" })); expect(mocks.put).not.toHaveBeenCalled();
});
it("修改自己成功后清除当前会话，触发退出而非保留旧能力", async () => {
  mocks.authz.snapshot.username = "target"; mount(); await loaded();
  fireEvent.click(screen.getByRole("button", { name: "保存授权" }));
  await waitFor(() => expect(localStorage.getItem("token")).toBeNull());
  expect(onSaved).not.toHaveBeenCalled(); expect(onClose).toHaveBeenCalledOnce();
});
it("无管理资格不展示也不请求目标授权", () => {
  mocks.authz.snapshot.can_manage = false; mount();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); expect(mocks.get).not.toHaveBeenCalled();
});
it("切换目标后旧账号迟到读取不能覆盖新账号", async () => {
  let resolve!: (value: { data: SalesAuthorization }) => void;
  mocks.get.mockReturnValueOnce(new Promise(r => { resolve = r; }));
  const view = render(<SalesAuthorizationDrawer key="first" username="first" onClose={onClose} onSaved={onSaved} />);
  view.rerender(<SalesAuthorizationDrawer key="target" username="target" onClose={onClose} onSaved={onSaved} />);
  await loaded(); await act(async () => resolve({ data: { ...initial, version: 88, customer_ids: [], customers: [] } }));
  expect(screen.getByLabelText("已确认负责客户")).toHaveTextContent("未在首屏的已选客户");
  fireEvent.click(screen.getByRole("button", { name: "保存授权" }));
  await waitFor(() => expect(mocks.put).toHaveBeenCalledWith("target", expect.objectContaining({ expected_version: 3 }), "owner"));
});
it("同规范名不同客户 ID 可分别勾选，已选列表仍可区分", async () => {
  mocks.search.mockResolvedValue({ data: { items: [
    { id: 1, name: "同名客户", raw_name: "同名客户北京" },
    { id: 2, name: "同名客户", raw_name: "同名客户上海" },
  ] } });
  mount(); await loaded(); const panel = await openPicker();
  fireEvent.click(await within(panel).findByRole("checkbox", { name: "同名客户（ID 1）" }));
  fireEvent.click(within(panel).getByRole("checkbox", { name: "同名客户（ID 2）" }));
  expect(within(panel).getByText("原始名：同名客户北京")).toBeInTheDocument();
  expect(within(panel).getByRole("button", { name: "移除同名客户（ID 1）" })).toBeInTheDocument();
  expect(within(panel).getByRole("button", { name: "移除同名客户（ID 2）" })).toBeInTheDocument();
  fireEvent.click(within(panel).getByRole("button", { name: "确定" }));
  expect(screen.getByLabelText("已确认负责客户")).toHaveTextContent("同名客户（ID 1）");
  expect(screen.getByLabelText("已确认负责客户")).toHaveTextContent("同名客户（ID 2）");
  fireEvent.click(screen.getByRole("button", { name: "保存授权" }));
  await waitFor(() => expect(mocks.put).toHaveBeenCalledWith("target", expect.objectContaining({ customer_ids: [53, 1, 2] }), "owner"));
});
