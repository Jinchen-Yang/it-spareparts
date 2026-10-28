import { expect } from "vitest";
import { fireEvent, screen, waitFor } from "../../../test/antdRender";

/** 测试通过显式行操作打开 Drawer，业务断言不依赖表格的 DOM 结构。 */
export async function closeLine() {
  const close = document.querySelector<HTMLButtonElement>(".ant-drawer-open .ant-drawer-close");
  if (close) {
    fireEvent.click(close);
    await waitFor(() => expect(document.querySelector(".ant-drawer-open")).toBeNull());
  }
}

export async function openLine(pn: string, binding = false) {
  const drawer = document.querySelector(".ant-drawer-open");
  if (drawer?.querySelector(".ant-drawer-title")?.textContent?.startsWith(`${pn} ·`) && (!binding || drawer.textContent?.includes("先查重"))) return;
  await closeLine();
  const label = binding ? `建档 ${pn}` : new RegExp(`^(填写 ${pn.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")} 报价|编辑 ${pn.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}|查看 ${pn.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")} 详情)$`);
  fireEvent.click(await screen.findByRole("button", { name: label }));
}
