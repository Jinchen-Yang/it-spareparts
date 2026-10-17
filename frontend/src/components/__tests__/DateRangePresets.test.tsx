// 日期预设（D-39 / Q10）纯函数与组件行为：
// 各预设 → 闭区间本地日期；「全部」= null（不传日期）；跨月/闰年边界按日历月钉回月末；
// 自定义非法回退今天；组件切换预设回调携带完整 {preset, from, to}。
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import dayjs from "dayjs";

import DateRangePresets, {
  DATE_PRESET_OPTIONS,
  DEFAULT_DATE_PRESET,
  isDatePreset,
  resolveDateRange,
  resolvePresetRange,
} from "../DateRangePresets";

const d = (s: string) => dayjs(s);

afterEach(cleanup);

describe("resolvePresetRange：预设 → 闭区间（本地日期）", () => {
  it("固定日预设：今天/昨天/前天", () => {
    expect(resolvePresetRange("today", d("2026-10-17"))).toEqual({
      from: "2026-10-17",
      to: "2026-10-17",
    });
    expect(resolvePresetRange("yesterday", d("2026-10-17"))).toEqual({
      from: "2026-10-16",
      to: "2026-10-16",
    });
    expect(resolvePresetRange("before_yesterday", d("2026-10-17"))).toEqual({
      from: "2026-10-15",
      to: "2026-10-15",
    });
    // 月初跨月：10-01 的昨天落在 9 月
    expect(resolvePresetRange("yesterday", d("2026-10-01"))).toEqual({
      from: "2026-09-30",
      to: "2026-09-30",
    });
    // 年初跨年：1 月 1 日的前天落在上一年
    expect(resolvePresetRange("before_yesterday", d("2026-01-01"))).toEqual({
      from: "2025-12-30",
      to: "2025-12-30",
    });
  });

  it("滚动窗口：N 天含今天（与 boss 看板/旧采购分析后端口径一致）", () => {
    expect(resolvePresetRange("7d", d("2026-10-17"))).toEqual({
      from: "2026-10-11",
      to: "2026-10-17",
    });
    expect(resolvePresetRange("15d", d("2026-10-17"))).toEqual({
      from: "2026-10-03",
      to: "2026-10-17",
    });
    expect(resolvePresetRange("30d", d("2026-10-17"))).toEqual({
      from: "2026-09-18",
      to: "2026-10-17",
    });
    // 跨月：03-05 的 7 天窗口起点落在 2 月（2024 闰年，2 月有 29 天；恰好 7 个日历天）
    expect(resolvePresetRange("7d", d("2024-03-05"))).toEqual({
      from: "2024-02-28",
      to: "2024-03-05",
    });
    // 跨年：01-15 的 30 天窗口起点落在上一年 12 月（12-17 ~ 01-15 = 30 天）
    expect(resolvePresetRange("30d", d("2025-01-15"))).toEqual({
      from: "2024-12-17",
      to: "2025-01-15",
    });
    // 区间天数自查：7/15/30 预设各恰好 7/15/30 个日历天
    for (const [preset, n] of [["7d", 7], ["15d", 15], ["30d", 30]] as const) {
      const r = resolvePresetRange(preset, d("2026-10-17"))!;
      expect(d(r.to).diff(d(r.from), "day") + 1, preset).toBe(n);
    }
  });

  it("日历月/年相减：溢出日期钉回目标月末（闰年边界）", () => {
    // 闰日 − 1 年：2023 不是闰年 → 02-28
    expect(resolvePresetRange("1y", d("2024-02-29"))).toEqual({
      from: "2023-02-28",
      to: "2024-02-29",
    });
    // 月末 − 6 月：03-31 → 09-30
    expect(resolvePresetRange("6m", d("2024-03-31"))).toEqual({
      from: "2023-09-30",
      to: "2024-03-31",
    });
    // 闰年 8 月末 − 6 月 → 闰 2 月末 02-29
    expect(resolvePresetRange("6m", d("2024-08-31"))).toEqual({
      from: "2024-02-29",
      to: "2024-08-31",
    });
    // 平年常态：10-17 − 1 年 / − 6 月
    expect(resolvePresetRange("1y", d("2026-10-17"))).toEqual({
      from: "2025-10-17",
      to: "2026-10-17",
    });
    expect(resolvePresetRange("6m", d("2026-10-17"))).toEqual({
      from: "2026-04-17",
      to: "2026-10-17",
    });
  });

  it("「全部」与「custom」返回 null（custom 的实际区间由 resolveDateRange 兜底）", () => {
    expect(resolvePresetRange("all", d("2026-10-17"))).toBeNull();
    expect(resolvePresetRange("custom", d("2026-10-17"))).toBeNull();
  });
});

describe("resolveDateRange：自定义区间兜底", () => {
  it("合法自定义区间原样返回（from ≤ to）", () => {
    expect(resolveDateRange("custom", "2026-09-01", "2026-10-15", d("2026-10-17"))).toEqual({
      from: "2026-09-01",
      to: "2026-10-15",
    });
    // from = to 是合法闭区间
    expect(resolveDateRange("custom", "2026-10-01", "2026-10-01", d("2026-10-17"))).toEqual({
      from: "2026-10-01",
      to: "2026-10-01",
    });
  });

  it("非法自定义回退今天：不存在的日期 / 非严格 ISO / from > to / 缺边", () => {
    const today = d("2026-10-17");
    // dayjs 会把 2026-02-31 滚成 3 月，必须拒绝而不是静默滚动
    expect(resolveDateRange("custom", "2026-02-31", "2026-03-05", today))
      .toEqual({ from: "2026-10-17", to: "2026-10-17" });
    expect(resolveDateRange("custom", "2026-9-1", "2026-10-15", today))
      .toEqual({ from: "2026-10-17", to: "2026-10-17" });
    expect(resolveDateRange("custom", "2026-10-15", "2026-09-01", today))
      .toEqual({ from: "2026-10-17", to: "2026-10-17" });
    expect(resolveDateRange("custom", null, "2026-10-15", today))
      .toEqual({ from: "2026-10-17", to: "2026-10-17" });
    expect(resolveDateRange("custom", "2026-10-15", null, today))
      .toEqual({ from: "2026-10-17", to: "2026-10-17" });
  });

  it("非自定义预设委托 resolvePresetRange（「全部」= null，即请求不传日期）", () => {
    expect(resolveDateRange("all", "2026-01-01", "2026-02-01", d("2026-10-17"))).toBeNull();
    expect(resolveDateRange("today", null, null, d("2026-10-17")))
      .toEqual({ from: "2026-10-17", to: "2026-10-17" });
  });
});

describe("预设枚举", () => {
  it("默认今天（Q10）；选项覆盖 D-39 全部预设 + 自定义", () => {
    expect(DEFAULT_DATE_PRESET).toBe("today");
    expect(DATE_PRESET_OPTIONS.map((o) => o.value)).toEqual([
      "all", "1y", "6m", "30d", "15d", "7d", "today", "yesterday", "before_yesterday", "custom",
    ]);
    expect(DATE_PRESET_OPTIONS.map((o) => o.label)).toEqual([
      "全部", "一年", "半年", "30 天", "15 天", "7 天", "今天", "昨天", "前天", "自定义",
    ]);
  });

  it("isDatePreset 只认已知预设（URL 里的野值不算）", () => {
    for (const v of ["all", "1y", "6m", "30d", "15d", "7d", "today", "yesterday", "before_yesterday", "custom"]) {
      expect(isDatePreset(v), v).toBe(true);
    }
    for (const v of ["", "last_week", "TODAY", null, undefined]) {
      expect(isDatePreset(v as string | null | undefined), String(v)).toBe(false);
    }
  });
});

describe("DateRangePresets 组件", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date(2026, 9, 17, 10, 0, 0));
  });
  afterEach(() => vi.useRealTimers());

  function Harness(props: Partial<Parameters<typeof DateRangePresets>[0]> = {}) {
    return (
      <MemoryRouter>
        <DateRangePresets preset="today" from={null} to={null} onChange={() => {}} {...props} />
      </MemoryRouter>
    );
  }

  const pick = async (label: string) => {
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "日期预设" }));
    fireEvent.click(await screen.findByText(label, { selector: ".ant-select-item-option-content" }));
  };

  it("切到普通预设：回调清空自定义区间（from/to = null）", async () => {
    const onChange = vi.fn();
    render(<Harness preset="custom" from="2026-09-01" to="2026-10-15" onChange={onChange} />);
    await pick("全部");
    expect(onChange).toHaveBeenLastCalledWith({ preset: "all", from: null, to: null });
  });

  it("切到自定义：把当前生效区间快照进去（不落空值）", async () => {
    const onChange = vi.fn();
    render(<Harness preset="7d" onChange={onChange} />);
    await pick("自定义");
    // 当前 7 天区间是 2026-10-11 ~ 2026-10-17（假时钟，7 天含今天），快照进自定义初值
    expect(onChange).toHaveBeenLastCalledWith({
      preset: "custom",
      from: "2026-10-11",
      to: "2026-10-17",
    });
  });

  it("「全部」之下不渲染 RangePicker；自定义时渲染", async () => {
    const { container, rerender } = render(<Harness preset="all" />);
    expect(container.querySelector(".ant-picker")).toBeNull();
    rerender(
      <MemoryRouter>
        <DateRangePresets
          preset="custom" from="2026-09-01" to="2026-10-15"
          onChange={() => {}}
        />
      </MemoryRouter>,
    );
    // RangePicker 起止两个日期输入框
    expect(container.querySelectorAll(".ant-picker input").length).toBe(2);
  });
});
