/** PR #440 第 2 轮返工（sol M4 / astra B4）：恢复清单状态标签对比度。
 *
 * 「可恢复」「冲突」「已恢复」「不可自动恢复」前景/背景对比度必须 ≥ 4.5:1
 * （WCAG AA 正文标准）。antd 预设 orange/green/red 在真实浏览器实测只有
 * 3.3~3.4:1（验收报告 M4），且 jsdom 解析不到预设变体规则、无法兜底测量，
 * 因此三个语义状态标签必须走 TAG_STYLES 主题 token 内联着色（仓库既有
 * 模式，如 ProjectCard），测试直接实测 token 的前景/背景；「不可自动恢复」
 * 沿用 antd default 预设（实测 14.25:1），按级联样式实测。
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  list: vi.fn(), preview: vi.fn(), restore: vi.fn(),
}));
vi.mock("../src/api/maintenanceSourceCancelCascade", () => ({
  listSourceRestorations: api.list,
  getSourceRestoration: api.preview,
  restoreSourceCancellation: api.restore,
}));
import SourceCancelRestorationSection from "../src/components/maintenance/SourceCancelRestorationSection";

type Rgba = [number, number, number, number];

const parseColor = (value: string): Rgba | null => {
  const match = value.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+))?\s*\)$/);
  if (!match) return null;
  return [Number(match[1]), Number(match[2]), Number(match[3]),
          match[4] === undefined ? 1 : Number(match[4])];
};

/** 半透明前景先按 alpha 与背景合成，再算亮度（antd 文本色常带 0.88 透明度）。 */
const compositeOver = (foreground: Rgba, background: Rgba): [number, number, number] =>
  foreground.slice(0, 3).map(
    (channel, index) => channel * foreground[3] + background[index] * (1 - foreground[3]),
  ) as [number, number, number];

const srgbChannel = (value: number) => {
  const ratio = value / 255;
  return ratio <= 0.03928 ? ratio / 12.92 : ((ratio + 0.055) / 1.055) ** 2.4;
};

const luminance = ([r, g, b]: [number, number, number]) =>
  0.2126 * srgbChannel(r) + 0.7152 * srgbChannel(g) + 0.0722 * srgbChannel(b);

const contrastRatio = (foregroundText: string, backgroundText: string) => {
  const foreground = parseColor(foregroundText);
  let background = parseColor(backgroundText);
  if (!foreground || !background) return null;
  if (background[3] < 1) background = [...compositeOver(background, [255, 255, 255, 1]), 1] as Rgba;
  const solid: [number, number, number] = foreground[3] < 1
    ? compositeOver(foreground, background)
    : [foreground[0], foreground[1], foreground[2]];
  const [lighter, darker] = [luminance(solid), luminance([background[0], background[1], background[2]])]
    .sort((a, b) => b - a);
  return (lighter + 0.05) / (darker + 0.05);
};

const baseItem = {
  batch_id: 8, source_order_id: "private-order-id", order_no: "WBDD-20261020-0001",
  confirmed_at: "2026-10-20T09:00:00Z", confirmed_by: "cancel-admin",
  confirm_reason: "已核对源单取消", voided_lines: 2, voided_site_lines: 1, voided_site_issues: 1,
  warnings: [], conflicts: [], restored: false, restore_receipt: null,
  expected_digest: null,
};

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  localStorage.setItem("token", "token-1");
  api.list.mockResolvedValue({ data: { items: [
    { ...baseItem, state: "restorable" },
    { ...baseItem, source_order_id: "order-conflict", state: "conflict",
      conflicts: [{ kind: "receipt", id: "order-conflict", reason: "示例冲突" }] },
    { ...baseItem, source_order_id: "order-restored", state: "restored", restored: true,
      restore_receipt: { batch_id: 8, operated_by: "cancel-admin", restored_at: "2026-10-21T09:30:00Z" } },
    { ...baseItem, source_order_id: "order-unavailable", state: "unavailable",
      conflicts: [{ kind: "receipt", id: "order-unavailable", reason: "回执恢复快照损坏，不可自动恢复" }] },
  ], total: 4 } });
});
afterEach(cleanup);

const findStateTag = async (label: string) => {
  await waitFor(() => expect(screen.getAllByText(label).length).toBeGreaterThan(0));
  const tag = screen.getAllByText(label)
    .map((node) => node.closest(".ant-tag"))
    .filter((node): node is HTMLElement => node !== null)
    .find((node) => node.textContent === label);
  expect(tag, `未找到文本为 ${label} 的状态标签`).toBeDefined();
  return tag;
};

describe("恢复清单状态标签对比度（WCAG AA ≥ 4.5:1）", () => {
  it.each([
    ["可恢复", "warning"], ["冲突", "danger"], ["已恢复", "success"],
  ] as const)("语义标签 %s 由主题 token 着色且对比度达标", async (label) => {
    render(<SourceCancelRestorationSection
      projectId="project-1" canConfirm sessionToken="token-1" onChanged={vi.fn().mockResolvedValue(true)} />);
    const tag = await findStateTag(label);
    const inline = tag.style;
    // jsdom 解析不到 antd 预设变体色；语义标签必须内联主题 token 才可实测，
    // 而 orange/green/red 预设实测不达标（验收 M4），不得回退到预设。
    expect(inline.color, `${label} 未使用主题 token 内联着色`).not.toBe("");
    expect(inline.backgroundColor, `${label} 未使用主题 token 内联着色`).not.toBe("");
    const ratio = contrastRatio(inline.color, inline.backgroundColor);
    expect(ratio, `${label} 样式无法解析：color=${inline.color} background=${inline.backgroundColor}`).not.toBeNull();
    expect(ratio!, `${label} 对比度 ${ratio?.toFixed(3)} 低于 4.5`).toBeGreaterThanOrEqual(4.5);
  });

  it("「不可自动恢复」沿用 antd 预设且对比度达标", async () => {
    render(<SourceCancelRestorationSection
      projectId="project-1" canConfirm sessionToken="token-1" onChanged={vi.fn().mockResolvedValue(true)} />);
    const tag = await findStateTag("不可自动恢复");
    const style = window.getComputedStyle(tag);
    const ratio = contrastRatio(style.color, style.backgroundColor);
    expect(ratio, `color=${style.color} background=${style.backgroundColor}`).not.toBeNull();
    expect(ratio!, `不可自动恢复 对比度 ${ratio?.toFixed(3)} 低于 4.5`).toBeGreaterThanOrEqual(4.5);
  });
});
