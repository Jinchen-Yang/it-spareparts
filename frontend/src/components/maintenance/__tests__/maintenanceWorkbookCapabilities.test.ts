import { describe, expect, it } from "vitest";
import { resolveMaintenanceWorkbookCapabilities } from "../maintenancePermissions";

const reader = { page_maintenance: true, data_profit: true, data_purchase_cost: true };
const uploader = { ...reader, action_maintenance_expense_collection_upload: true };

describe("稳定工作簿用途的权限边界", () => {
  it.each([
    { name: "只有页面", permissions: { page_maintenance: true }, download: false, upload: false },
    { name: "采购仅成本", permissions: { page_maintenance: true, data_purchase_cost: true }, download: false, upload: false },
    { name: "利润阅读", permissions: reader, download: true, upload: false },
    { name: "完整上传", permissions: uploader, download: true, upload: true },
    { name: "上传 action 残留但无金额", permissions: { page_maintenance: true, action_maintenance_expense_collection_upload: true }, download: false, upload: false },
    { name: "利润键残留但成本被撤销", permissions: { ...uploader, data_purchase_cost: false }, download: false, upload: false },
    { name: "页面已撤销", permissions: { ...uploader, page_maintenance: false }, download: false, upload: false },
  ])("$name：全局及旧载荷回退遵循真实 API 资格", ({ permissions, download, upload }) => {
    const capabilities = resolveMaintenanceWorkbookCapabilities({ role: "sales", permissions, project: {} });
    expect(capabilities.global).toEqual({ canDownload: download, canUpload: upload });
    expect(capabilities.project).toEqual({ canDownload: download, canUpload: upload });
  });

  it.each(["detailCanEdit", "boardCanEdit"] as const)("负责人通过 %s 获得本人项目资格，不扩大到全局", (source) => {
    const capabilities = resolveMaintenanceWorkbookCapabilities({
      role: "maintenance_manager", permissions: { page_maintenance: true }, project: { [source]: true },
    });
    expect(capabilities.project).toEqual({ canDownload: true, canUpload: true });
    expect(capabilities.global).toEqual({ canDownload: false, canUpload: false });
  });

  it.each([
    { detailCanEdit: false }, { boardCanEdit: false },
    { detailCanEdit: false, boardCanEdit: true }, { detailCanEdit: true, boardCanEdit: false },
  ])("服务端明确拒绝优先于本地 action 和另一旧正值：%j", (project) => {
    const capabilities = resolveMaintenanceWorkbookCapabilities({ role: "sales", permissions: uploader, project });
    expect(capabilities.project).toEqual({ canDownload: true, canUpload: false });
    expect(capabilities.global.canUpload).toBe(true);
  });

  it("项目角色不代替服务端归属，未加载项目不提供项目入口", () => {
    for (const role of ["sales", "maintenance_manager"]) {
      expect(resolveMaintenanceWorkbookCapabilities({ role, permissions: { page_maintenance: true }, project: {} }).project)
        .toEqual({ canDownload: false, canUpload: false });
    }
    expect(resolveMaintenanceWorkbookCapabilities({ role: "sales", permissions: uploader }).project)
      .toEqual({ canDownload: false, canUpload: false });
  });

  it("项目例外不覆盖页面撤销，管理员保留常规绕过", () => {
    expect(resolveMaintenanceWorkbookCapabilities({ role: "sales", permissions: {}, project: { detailCanEdit: true } }).project)
      .toEqual({ canDownload: false, canUpload: false });
    const admin = resolveMaintenanceWorkbookCapabilities({ role: "admin", permissions: {}, project: {} });
    expect(admin.global).toEqual({ canDownload: true, canUpload: true });
    expect(admin.project).toEqual({ canDownload: true, canUpload: true });
  });

  it("没有 data_customer 或具有销售范围限制不误套旧 roundtrip 下载门", () => {
    expect(resolveMaintenanceWorkbookCapabilities({
      role: "sales", permissions: { ...uploader, data_customer: false, own_customers_only: true }, project: {},
    }).global).toEqual({ canDownload: true, canUpload: true });
  });
});
