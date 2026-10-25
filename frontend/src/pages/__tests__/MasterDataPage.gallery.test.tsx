import { Grid } from "antd";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanupAntd, fireEvent, render, screen, waitFor, within } from "../../test/antdRender";
import { api } from "../../api";
import { invalidateCover } from "../../api/galleryCovers";
import MasterDataPage from "../MasterDataPage";

const galleryCallback = vi.hoisted(() => ({ current: undefined as (() => void) | undefined }));

vi.mock("../../api/galleryCovers", () => ({ invalidateCover: vi.fn() }));

vi.mock("../../components/gallery/PartThumb", () => ({
  default: ({ partId, pn, onEmptyClick }: {
    partId: number; pn: string; onEmptyClick?: () => void;
  }) => (
    <button type="button" aria-label={`${pn} 空缩略图`} data-testid={`thumb-${pn}`}
      data-part-id={partId} onClick={onEmptyClick} />
  ),
}));

vi.mock("../../components/parts/PartGalleryPanel", () => ({
  default: ({ partId, pn, canWrite, onChanged }: {
    partId: number; pn: string; canWrite: boolean; onChanged?: () => void;
  }) => {
    galleryCallback.current = onChanged;
    return (
      <div data-testid="editor-gallery" data-part-id={partId} data-pn={pn} data-can-write={canWrite}>
        <button type="button" onClick={onChanged}>模拟图库修改成功</button>
      </div>
    );
  },
}));

const originalAdapter = api.defaults.adapter;
const fields = {
  description: "企业硬盘描述", brand: "Seagate", manufacturer: "希捷制造商", summary: "企业级存储简介",
  category_major: "存储", category_minor: "硬盘", machine_or_part: "备件", unit: "个",
};
const rows = [
  { id: 7, part_id: 42, pn_std: "PN-42", ...fields, needs_review: true },
  { id: 43, pn_std: "PN-43", description: "缺少扩展资料", manufacturer: null, summary: null },
  { pn_std: "PN-NO-ID", description: "没有稳定型号标识" },
];

function snapshot(partId: number) {
  return {
    part_id: partId, pn_std: `PN-${partId}`,
    identity: { part_id: partId, pn_std: `PN-${partId}`, status: "active", merged_into_id: null },
    fields, specs: {}, version_mode: "field_snapshot",
  };
}

function response(config: any, data: unknown) {
  return { data, status: 200, statusText: "OK", headers: {}, config };
}

function renderPage() {
  return render(<MemoryRouter><MasterDataPage /></MemoryRouter>);
}

async function openEdit(pn = "PN-42") {
  const row = await screen.findByRole("row", { name: new RegExp(pn) });
  fireEvent.click(within(row).getByRole("button", { name: /编\s*辑/ }));
  const dialog = await screen.findByRole("dialog", { name: /编辑 PN/ });
  await within(dialog).findByDisplayValue(fields.manufacturer);
  return dialog;
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("token", "master-gallery-token");
  localStorage.setItem("role", "admin");
  vi.spyOn(Grid, "useBreakpoint").mockReturnValue({ xl: true });
  vi.mocked(invalidateCover).mockClear();
  galleryCallback.current = undefined;
  api.defaults.adapter = async (config) => {
    if (config.url === "/parts/search") return response(config, { items: rows, total: rows.length });
    const partId = config.url?.match(/^\/parts\/master\/(\d+)\/edit-state$/)?.[1];
    if (partId) return response(config, snapshot(Number(partId)));
    if (config.url === "/parts/master/categories") return response(config, {
      categories: [{ name: "存储", children: [{ name: "硬盘" }] }],
    });
    throw new Error(`unexpected ${config.method} ${config.url}`);
  };
});

afterEach(async () => {
  await cleanupAntd();
  api.defaults.adapter = originalAdapter;
  vi.restoreAllMocks();
});

it("主数据列表依次展示图片、制造商与简介，空值占位并连接两级品类", async () => {
  renderPage();
  const populated = await screen.findByRole("row", { name: /PN-42/ });
  expect(screen.getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual([
    "图片", "PN", "描述", "品牌", "制造商", "品类", "简介", "操作",
  ]);
  expect(within(populated).getByText(fields.manufacturer)).toBeInTheDocument();
  expect(within(populated).getByText(fields.summary)).toBeInTheDocument();
  expect(within(populated).getByText("存储 / 硬盘")).toBeInTheDocument();
  const empty = screen.getByRole("row", { name: /PN-43/ });
  const cells = within(empty).getAllByRole("cell");
  expect(cells[4]).toHaveTextContent(/^-$/);
  expect(cells[6]).toHaveTextContent(/^-$/);
  expect(within(empty).getByText("未分类")).toBeInTheDocument();
  expect(screen.getByRole("table").closest(".ant-table-content")).toHaveStyle({ overflowX: "auto" });
});

it("缩略图优先使用 part_id，兼容 id，缺失两者时只显示占位", async () => {
  renderPage();
  expect(await screen.findByTestId("thumb-PN-42")).toHaveAttribute("data-part-id", "42");
  expect(screen.getByTestId("thumb-PN-43")).toHaveAttribute("data-part-id", "43");
  const missing = screen.getByRole("row", { name: /PN-NO-ID/ });
  expect(within(missing).queryByRole("button", { name: /空缩略图/ })).toBeNull();
  expect(screen.queryByTestId("thumb-PN-NO-ID")).toBeNull();
});

it("宽屏编辑使用 Drawer 同时展示资料与图库，PN 与待复核显示在头部且底部保留操作", async () => {
  renderPage();
  const dialog = await openEdit();
  expect(dialog.closest(".ant-drawer")).toBeInTheDocument();
  expect(within(dialog).getByText("PN-42", { selector: ".ant-tag" })).toBeInTheDocument();
  expect(within(dialog).getByText("待复核", { selector: ".ant-tag" })).toHaveClass("ant-tag-orange");
  expect(within(dialog).getByRole("region", { name: "资料" })).toBeVisible();
  expect(within(dialog).getByRole("region", { name: "图库" })).toBeVisible();
  expect(within(dialog).queryByRole("tablist")).toBeNull();
  const gallery = within(dialog).getByTestId("editor-gallery");
  expect(gallery).toHaveAttribute("data-part-id", "42");
  expect(gallery).toHaveAttribute("data-pn", "PN-42");
  const footer = dialog.querySelector<HTMLElement>(".ant-drawer-footer")!;
  expect(within(footer).getByRole("button", { name: /保\s*存/ })).toBeEnabled();
  expect(within(footer).getByRole("button", { name: /取\s*消/ })).toBeInTheDocument();
});

it("窄屏编辑默认资料页签，可切换到图库", async () => {
  vi.mocked(Grid.useBreakpoint).mockReturnValue({ xl: false });
  renderPage();
  const dialog = await openEdit();
  const details = within(dialog).getByRole("tab", { name: "资料" });
  const gallery = within(dialog).getByRole("tab", { name: "图库" });
  expect(details).toHaveAttribute("aria-selected", "true");
  expect(gallery).toHaveAttribute("aria-selected", "false");
  fireEvent.click(gallery);
  expect(gallery).toHaveAttribute("aria-selected", "true");
  expect(within(dialog).getByTestId("editor-gallery")).toBeVisible();
  expect(within(dialog).getByLabelText("描述")).not.toBeVisible();
});

it("从空缩略图进入窄屏编辑时直接选中图库页签", async () => {
  vi.mocked(Grid.useBreakpoint).mockReturnValue({ xl: false });
  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "PN-42 空缩略图" }));
  const dialog = await screen.findByRole("dialog", { name: /编辑 PN/ });
  expect(within(dialog).getByRole("tab", { name: "图库" })).toHaveAttribute("aria-selected", "true");
  expect(within(dialog).getByTestId("editor-gallery")).toBeVisible();
});

it("新建抽屉提示保存后上传，未填写 PN 时禁用保存且不挂载图库组件", async () => {
  renderPage();
  fireEvent.click(screen.getByRole("button", { name: "新建 PN" }));
  const dialog = await screen.findByRole("dialog", { name: "新建 PN" });
  expect(dialog.closest(".ant-drawer")).toBeInTheDocument();
  expect(within(dialog).getByText("保存后即可上传图片")).toBeVisible();
  expect(within(dialog).queryByTestId("editor-gallery")).toBeNull();
  expect(within(dialog).getByRole("button", { name: /保\s*存/ })).toBeDisabled();
});

it("图库成功修改后关闭抽屉才清除该 part_id 的封面缓存", async () => {
  renderPage();
  const dialog = await openEdit();
  fireEvent.click(within(dialog).getByRole("button", { name: "模拟图库修改成功" }));
  expect(invalidateCover).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole("button", { name: /取\s*消/ }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(invalidateCover).toHaveBeenCalledExactlyOnceWith(42);
  const unchanged = await openEdit();
  fireEvent.click(within(unchanged).getByRole("button", { name: /取\s*消/ }));
  expect(invalidateCover).toHaveBeenCalledTimes(1);
});

it("关闭抽屉后图库写入成功响应迟到，仍清除原 part_id 的封面缓存", async () => {
  renderPage();
  const dialog = await openEdit();
  const delayedChanged = galleryCallback.current!;
  fireEvent.click(within(dialog).getByRole("button", { name: /取\s*消/ }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(invalidateCover).not.toHaveBeenCalled();

  act(() => { delayedChanged(); });

  expect(invalidateCover).toHaveBeenCalledExactlyOnceWith(42);
});

it.each([true, false])("图库维护权限跟随 page_master_data=%s", async (canWrite) => {
  localStorage.setItem("role", "purchaser");
  localStorage.setItem("permissions", JSON.stringify({ page_master_data: canWrite }));
  renderPage();
  const dialog = await openEdit();
  expect(within(dialog).getByTestId("editor-gallery")).toHaveAttribute("data-can-write", String(canWrite));
});

it("新建近似 PN 保留资料草稿，确认不重复后才携带 force=true 再提交", async () => {
  const original = api.defaults.adapter as (config: any) => Promise<any>;
  const creates: any[] = [];
  api.defaults.adapter = async (config) => {
    if (config.method === "post" && config.url === "/parts/master") {
      const payload = JSON.parse(config.data);
      creates.push(payload);
      return response(config, payload.force
        ? { created: true, pn_std: "NEW-PN" }
        : { created: false, near_duplicates: [{ pn_std: "OLD-PN", reason: "型号近似" }] });
    }
    return original(config);
  };
  renderPage();
  fireEvent.click(screen.getByRole("button", { name: "新建 PN" }));
  const dialog = await screen.findByRole("dialog", { name: "新建 PN" });
  fireEvent.change(within(dialog).getByLabelText("PN（产品唯一标识）"), { target: { value: " NEW-PN " } });
  fireEvent.change(within(dialog).getByLabelText("制造商"), { target: { value: "新建制造商" } });
  fireEvent.change(within(dialog).getByLabelText("简介"), { target: { value: "新建简介" } });
  fireEvent.click(within(dialog).getByRole("button", { name: /保\s*存/ }));
  const force = await within(dialog).findByRole("button", { name: "确认不重复，强制新建" });
  expect(creates).toHaveLength(1);
  expect(creates[0]).toMatchObject({ pn_std: "NEW-PN", manufacturer: "新建制造商", summary: "新建简介", force: false });
  expect(within(dialog).getByLabelText("制造商")).toHaveValue("新建制造商");
  fireEvent.click(force);
  await waitFor(() => expect(creates).toHaveLength(2));
  expect(creates[1]).toEqual({ ...creates[0], force: true });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});
