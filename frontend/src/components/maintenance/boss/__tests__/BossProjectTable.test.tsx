import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import BossProjectTable from "../BossProjectTable";
import { UNASSIGNED_BUCKET } from "../../../../api/maintenanceBossBoard";
import type { BoardProjectRow } from "../../../../api/maintenanceBossBoard";

afterEach(cleanup);

const stat = (value: number) => ({ state: "ready" as const, value, as_of: null });

function makeRow(id: string, name = id): BoardProjectRow {
  return {
    project_id: id,
    project_code: name,
    display_name: name,
    lifecycle: "ongoing",
    is_archived: false,
    has_activity_in_window: true,
    pre_delivery_order_count: 0,
    orders_ytd: stat(1),
    lines_ytd: stat(1),
    known_apply_cost_inc_tax: { state: "restricted", value: null, as_of: null },
    shipped_qty: stat(1),
    returned_good_qty: stat(1),
    returned_bad_qty: stat(1),
  } as BoardProjectRow;
}

const bucketRow: BoardProjectRow = {
  ...makeRow(UNASSIGNED_BUCKET, "未归属（待人工确认）"),
  lifecycle: "missing",
};

function renderTable(props: Partial<Parameters<typeof BossProjectTable>[0]> = {}) {
  const onChange = vi.fn();
  const rows = props.rows ?? [bucketRow, ...Array.from({ length: 20 }, (_, i) =>
    makeRow(`p${i + 1}`, `项目${i + 1}`))];
  render(
    <MemoryRouter>
      <BossProjectTable
        rows={rows}
        total={props.total ?? 25}
        page={props.page ?? 1}
        pageSize={props.pageSize ?? 20}
        onChange={onChange}
      />
    </MemoryRouter>,
  );
  return { onChange };
}

describe("BossProjectTable 分页", () => {
  it("翻页回传的是请求的 page_size，不是当页行数", () => {
    // 回归：桶行是后端额外置顶的第 21 行。曾把 pageSize 谎报成 rows.length 来躲开
    // antd 客户端切片，rc-pagination 会把这个假值原样回吐，翻页请求带
    // page_size=21，offset 算成 21，第 21 个真实项目哪一页都不出现。
    const { onChange } = renderTable();
    fireEvent.click(screen.getByTitle("2"));
    expect(onChange).toHaveBeenCalledWith(2, 20);
  });

  it("当页行数少于 page_size 时也不改写 page_size", () => {
    const { onChange } = renderTable({
      rows: [makeRow("p21", "项目21"), makeRow("p22", "项目22")],
      page: 2,
      total: 25,
    });
    fireEvent.click(screen.getByTitle("1"));
    expect(onChange).toHaveBeenCalledWith(1, 20);
  });

  it("总数文案只在桶行真的在场时才承诺它", () => {
    renderTable();
    expect(screen.getByText(/未归属单另置顶一行/)).toBeInTheDocument();
    cleanup();
    // 经理范围/搜索/非首页时后端不注入桶行，文案不得再承诺
    renderTable({ rows: [makeRow("p1", "项目1")], total: 1 });
    expect(screen.queryByText(/未归属单另置顶一行/)).toBeNull();
    expect(screen.getByText(/共 1 个项目/)).toBeInTheDocument();
  });

  it("归档但仍带单的项目打「已归档」标", () => {
    renderTable({
      rows: [{ ...makeRow("p9", "归档项目"), is_archived: true }],
      total: 1,
    });
    expect(screen.getByText("已归档")).toBeInTheDocument();
  });
});
