import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import WbddUploadFeedback from "../WbddUploadFeedback";

afterEach(cleanup);

describe("原件上传聚合回执", () => {
  it("兼容旧回执，不把未提供的实际变更和保护计数当成零", () => {
    render(<WbddUploadFeedback report={{ batch_id: 8, fact_rows_updated: 4 }} />);

    expect(screen.getByText("本批明细处理行数（不含单头状态、地址变化）"))
      .toBeInTheDocument();
    expect(screen.getByText("匹配已有 4 行")).toBeInTheDocument();
    expect(screen.queryByText(/^实际变更 \d+ 行$/)).toBeNull();
    expect(screen.queryByText(/^无变化 \d+ 行$/)).toBeNull();
    expect(screen.queryByText(/^人工保护 \d+ 行$/)).toBeNull();
    expect(screen.getByText(/未提供的统计不代表 0/)).toBeInTheDocument();
  });

  it("跳过、实际变更与保护独立展示，白名单外的身份和值不进入反馈", () => {
    const report = {
      import_mode: "skip" as const, fact_rows_skipped: 5, fact_rows_semantic_updated: 0,
      fact_rows_protected: 2,
      protected_field_counts: { qty: 2, serial_numbers: 1, "private-source-identity": 99 },
      source_order_ids: ["private-order"],
      protected_samples: [{ source_line_id: "private-line", value: "private-value" }],
    };
    const { container } = render(<WbddUploadFeedback report={report} />);

    expect(screen.getByText("跳过模式：已有单据和明细不覆盖")).toBeInTheDocument();
    expect(screen.getByText("跳过 5 行")).toBeInTheDocument();
    expect(screen.getByText("实际变更 0 行")).toBeInTheDocument();
    expect(screen.getByText("人工保护 2 行")).toBeInTheDocument();
    expect(screen.getByText("需求数量 2 行")).toBeInTheDocument();
    expect(screen.getByText("SN 1 行")).toBeInTheDocument();
    expect(container).not.toHaveTextContent("private-");
    expect(container).not.toHaveTextContent("[object Object]");
  });
});
