import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "../../test/antdRender";
import PageHeader from "../PageHeader";
afterEach(cleanup);
it("标题、说明和动作保持可访问，不使用内联布局", () => {
  const onClick = vi.fn();
  const { container } = render(<PageHeader title="数据治理" subtitle="治理说明"
    extra={<button onClick={onClick}>刷新主数据</button>} />);
  expect(screen.getByRole("heading", { name: "数据治理", level: 1 })).toBeVisible();
  expect(screen.getByText("治理说明")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "刷新主数据" }));
  expect(onClick).toHaveBeenCalledOnce();
  expect(container.querySelector("[style]")).toBeNull();
});
