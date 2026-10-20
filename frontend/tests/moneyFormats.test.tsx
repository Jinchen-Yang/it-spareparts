import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { formatMoney, money, moneyExact, moneyIncTax } from "../src/utils/format";
import { fmtMoney as purchaseMoney } from "../src/pages/purchases/shared";
import { fmtMoney as salesMoney } from "../src/pages/sales/shared";
import { COLORS } from "../src/theme";
import { tokens } from "../src/tokens";
import KnownCostCell from "../src/components/maintenance/boss/KnownCostCell";
import type { KnownCostStat } from "../src/api/maintenanceBossBoard";

afterEach(cleanup);

describe("金额格式兼容（I17，预期取自各处旧输出）", () => {
  it.each([
    [null, "—"], [undefined, "—"], [0, "0.00"], [-0, "-0.00"],
    [1234.5, "1,234.50"], [-1234.567, "-1,234.57"], [1.005, "1.01"],
  ])("采购/销售 %s 保持币种、小数与空值: %s", (value, expected) => {
    expect(purchaseMoney(value)).toBe(expected);
    expect(salesMoney(value)).toBe(expected);
  });

  it("池管理仍用 toFixed 的舍入，不悄悄切成 Intl 舍入或千分位", () => {
    const options = { mode: "fixed", empty: "未设置" } as const;
    expect(formatMoney(1234.5, options)).toBe("1234.50");
    expect(formatMoney(-1234.567, options)).toBe("-1234.57");
    expect(formatMoney(1.005, options)).toBe("1.00");
    expect(formatMoney(-0, options)).toBe("0.00");
    expect(formatMoney(null, options)).toBe("未设置");
  });

  it("项目卡原始 Decimal 字符串保留尾零；回款整数与应收负号位置不变", () => {
    expect(formatMoney("100000.00", { mode: "raw", suffix: " 元" })).toBe("100000.00 元");
    expect(formatMoney("", { mode: "raw", suffix: " 元", emptyStringAsEmpty: true })).toBe("—");
    expect(formatMoney(30000.25, { prefix: "¥", maximumFractionDigits: 0 })).toBe("¥30,000");
    const receivable = { prefix: "¥", minimumFractionDigits: 0, signPosition: "before-prefix" } as const;
    expect(formatMoney(-10000.25, receivable)).toBe("-¥10,000.25");
    expect(formatMoney(-0, receivable)).toBe("¥0");
  });

  it("既有通用方法保持各自精度与 ¥- 负号顺序", () => {
    expect(money(null)).toBe("-");
    // 某些旧 API 的 Decimal JSON 实际是字符串；旧 money 的字符串 toLocaleString 不分组。
    expect(money("1234.50")).toBe("¥1234.50");
    expect(money("")).toBe("¥");
    expect(money(-1234.567)).toBe("¥-1,234.567");
    expect(moneyExact(-1234.567)).toBe("¥-1,234.57");
    expect(moneyIncTax(-1234.5)).toBe("¥-1,234.50");
    expect(moneyIncTax(undefined)).toBe("-");
  });

  it.each([["1234.5", "1,234.50"], ["错误值", "错误值"], ["", "0.00"], [null, "—"]])(
    "已知成本 %s 显示 %s（非法值仍保留来源，不造零）", (amount, expected) => {
      const stat = { state: "ready", as_of: null, value: {
        known_amount: amount, actual_amount: "0", estimated_amount: "0", missing_lines: 0,
        coverage_pct: null, quality: "actual_only",
      } } as KnownCostStat;
      render(<KnownCostCell stat={stat} compact />);
      expect(screen.getByTestId("cost-ready")).toHaveTextContent(expected);
    },
  );
});


describe("已知成本的语义色", () => {
  it("未导入用中性文字而非待复核色，缺价进度保留提醒色", () => {
    const { rerender, container } = render(<KnownCostCell stat={{ state: "not_imported", value: null, as_of: null }} />);
    expect(screen.getByTestId("cost-blocked")).toHaveTextContent("● 尚未导入");
    expect(screen.getByTestId("cost-blocked")).toHaveStyle({ color: tokens.color.text2 });
    expect(screen.getByTestId("cost-blocked")).not.toHaveStyle({ color: COLORS.status.pending });
    const stat: KnownCostStat = { state: "partial", as_of: null, value: {
      known_amount: "100.00", actual_amount: "50.00", estimated_amount: "50.00",
      missing_lines: 1, coverage_pct: 50, quality: "incomplete",
    } };
    rerender(<KnownCostCell stat={stat} />);
    expect(screen.getByTestId("cost-ready")).toHaveTextContent("100.00 ≥");
    expect(container.querySelector(".ant-progress-bg")).toHaveStyle({ backgroundColor: COLORS.warning });
  });
});
