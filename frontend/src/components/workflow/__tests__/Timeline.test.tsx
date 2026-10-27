import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";
import { cleanupAntd, fireEvent, render, screen, waitFor } from "../../../test/antdRender";
import { listSubjectEvents } from "../../../api/workItems";
import Timeline from "../Timeline";

type TimelineEvent = Parameters<NonNullable<ComponentProps<typeof Timeline>["renderEvent"]>>[0];

vi.mock("../../../api/workItems", () => ({ listSubjectEvents: vi.fn() }));

const oldEvent = {
  id: 1, subject_type: "demo", subject_id: 101, work_item_id: 11,
  kind: "claim", actor: 7, actor_name: "张工", payload: {},
  created_at: "2026-01-01T09:00:00Z",
};
const newEvent = {
  ...oldEvent, id: 2, kind: "complete", created_at: "2026-01-01T10:00:00Z",
};

function page(items = [oldEvent, newEvent], total = items.length, pageNumber = 1) {
  return { items, total, page: pageNumber, page_size: 20 };
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(listSubjectEvents).mockResolvedValue(page());
});

afterEach(cleanupAntd);

it("按事件 kind 显示操作人和中文动作", async () => {
  render(<Timeline subjectType="demo" subjectId={101} />);
  expect(await screen.findByText(/张工.*领取了待办/)).toBeInTheDocument();
  expect(screen.getByText(/张工.*完成了待办/)).toBeInTheDocument();
  expect(screen.getByText("2026-01-01 17:00")).toBeInTheDocument();
  expect(listSubjectEvents).toHaveBeenCalledWith(
    "demo", 101, expect.objectContaining({ page: 1 }), expect.any(AbortSignal),
  );
});

it("自定义事件渲染器收到完整事件，并按时间倒序显示", async () => {
  const renderEvent = vi.fn((event: TimelineEvent) => <span data-testid="event">{event.kind}:{event.id}</span>);
  render(<Timeline subjectType="demo" subjectId={101} renderEvent={renderEvent} />);
  await screen.findAllByTestId("event");
  expect(screen.getAllByTestId("event").map((node) => node.textContent)).toEqual(["complete:2", "claim:1"]);
  expect(renderEvent.mock.calls.map(([event]) => event)).toEqual(expect.arrayContaining([oldEvent, newEvent]));
  expect(screen.queryByText(/领取了待办/)).toBeNull();
});

it("加载更多追加较早事件并在全部加载后隐藏按钮", async () => {
  vi.mocked(listSubjectEvents)
    .mockResolvedValueOnce(page([newEvent], 2))
    .mockResolvedValueOnce(page([oldEvent], 2, 2));
  render(<Timeline subjectType="demo" subjectId={101}
    renderEvent={(event) => <span data-testid="event">事件 {event.id}</span>} />);
  fireEvent.click(await screen.findByRole("button", { name: /加载更多/ }));
  await waitFor(() => expect(listSubjectEvents).toHaveBeenLastCalledWith(
    "demo", 101, expect.objectContaining({ page: 2 }), expect.any(AbortSignal),
  ));
  expect(await screen.findByText("事件 1")).toBeInTheDocument();
  expect(screen.getAllByTestId("event").map((node) => node.textContent)).toEqual(["事件 2", "事件 1"]);
  expect(screen.queryByRole("button", { name: /加载更多/ })).toBeNull();
});

it("切换单据时清空上一单的事件并从第一页重新加载", async () => {
  vi.mocked(listSubjectEvents)
    .mockResolvedValueOnce(page([oldEvent]))
    .mockResolvedValueOnce(page([{ ...newEvent, subject_id: 102 }]));
  const view = render(<Timeline subjectType="demo" subjectId={101}
    renderEvent={(event) => <span>事件 {event.id}</span>} />);
  await screen.findByText("事件 1");
  view.rerender(<Timeline subjectType="demo" subjectId={102}
    renderEvent={(event) => <span>事件 {event.id}</span>} />);
  expect(await screen.findByText("事件 2")).toBeInTheDocument();
  expect(screen.queryByText("事件 1")).toBeNull();
  expect(listSubjectEvents).toHaveBeenLastCalledWith(
    "demo", 102, expect.objectContaining({ page: 1 }), expect.any(AbortSignal),
  );
});
