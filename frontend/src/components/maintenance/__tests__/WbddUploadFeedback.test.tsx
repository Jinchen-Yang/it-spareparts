import { legacyAuthorizationState, prepareLegacyPageFixture } from "../../../test/legacyAuthorization";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import WbddUploadFeedback from "../WbddUploadFeedback";

afterEach(cleanup);

describe("原件上传聚合回执", () => {
  it("兼容旧回执，不把未提供的实际变更和保护计数当成零", () => {
    render(<WbddUploadFeedback report={{ batch_id: 8, fact_rows_updated: 4 }} />);

    expect(screen.getByText("本批明细处理行数（不含单头状态、地址变化）"))
      .toBeInTheDocument();
    expect(screen.getByText("匹配已有 4 行")).toBeInTheDocument();
    expect(screen.getByText("实际变更 — 行")).toBeInTheDocument();
    expect(screen.getByText("无变化 — 行")).toBeInTheDocument();
    expect(screen.getByText("人工保护 — 行")).toBeInTheDocument();
    expect(screen.queryByText(/^实际变更 \d+ 行$/)).toBeNull();
    expect(screen.queryByText(/^无变化 \d+ 行$/)).toBeNull();
    expect(screen.queryByText(/^人工保护 \d+ 行$/)).toBeNull();
    expect(screen.getByText(/未提供的统计不代表 0/)).toBeInTheDocument();
  });

  it("显式 null 表示未评估，显示破折号并保留已知的零计数", () => {
    render(<WbddUploadFeedback report={{
      import_mode: "skip", fact_rows_skipped: 5, fact_rows_inserted: 0,
      fact_rows_semantic_updated: null, fact_rows_unchanged: null,
      fact_rows_protected: null, fact_rows_legacy_review: null,
      protected_field_counts: null,
    }} />);

    expect(screen.getByText("跳过 5 行")).toBeInTheDocument();
    expect(screen.getByText("新增 0 行")).toBeInTheDocument();
    expect(screen.getByText("实际变更 — 行")).toBeInTheDocument();
    expect(screen.getByText("无变化 — 行")).toBeInTheDocument();
    expect(screen.getByText("人工保护 — 行")).toBeInTheDocument();
    expect(screen.getByText("历史保护待核对 — 行")).toBeInTheDocument();
    expect(screen.queryByText(/^实际变更 0 行$/)).toBeNull();
  });

  it.each([
    [undefined, "—"],
    [null, "—"],
    [0, "0"],
    [2, "2"],
  ])("新增或变更单头独立按张展示（%s）", (count, display) => {
    render(<WbddUploadFeedback report={{
      orders_semantic_updated: count, fact_rows_semantic_updated: 0,
    }} />);

    expect(screen.getByText(`新增或变更单头 ${display} 张`)).toBeInTheDocument();
    expect(screen.getByText("实际变更 0 行")).toBeInTheDocument();
  });

  it("首次新增单头计入合并计数，明细变更仍单独展示", () => {
    render(<WbddUploadFeedback report={{
      orders_inserted: 1, orders_updated: 0, orders_semantic_updated: 1,
      fact_rows_semantic_updated: 0,
    }} />);

    expect(screen.getByText("新增或变更单头 1 张")).toBeInTheDocument();
    expect(screen.queryByText(/^单头实际变更/)).toBeNull();
    expect(screen.getByText("实际变更 0 行")).toBeInTheDocument();
  });

  it("实际变更与保护独立展示，白名单外的身份和值不进入反馈", () => {
    const report = {
      import_mode: "upsert" as const, fact_rows_skipped: 0, fact_rows_semantic_updated: 0,
      fact_rows_protected: 2,
      protected_field_counts: { qty: 2, serial_numbers: 1, "private-source-identity": 99 },
      source_order_ids: ["private-order"],
      protected_samples: [{ source_line_id: "private-line", value: "private-value" }],
    };
    const { container } = render(<WbddUploadFeedback report={report} />);

    expect(screen.getByText("更新模式：更新合法源值并保留人工保护")).toBeInTheDocument();
    expect(screen.getByText("跳过 0 行")).toBeInTheDocument();
    expect(screen.getByText("实际变更 0 行")).toBeInTheDocument();
    expect(screen.getByText("人工保护 2 行")).toBeInTheDocument();
    expect(screen.getByText("需求数量 2 行")).toBeInTheDocument();
    expect(screen.getByText("SN 1 行")).toBeInTheDocument();
    expect(container).not.toHaveTextContent("private-");
    expect(container).not.toHaveTextContent("[object Object]");
  });
});

// These existing business behavior fixtures intentionally exercise the legacy runtime.
vi.mock("../../../authz/AuthzProvider", async original => ({
  ...await original<typeof import("../../../authz/AuthzProvider")>(),
  useAuthz: () => legacyAuthorizationState(),
}));
beforeEach(() => prepareLegacyPageFixture());
