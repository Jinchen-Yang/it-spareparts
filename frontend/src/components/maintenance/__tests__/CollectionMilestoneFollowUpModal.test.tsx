import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createRef, type Ref, type RefObject } from "react";

const { followUpCollectionMilestone } = vi.hoisted(() => ({
  followUpCollectionMilestone: vi.fn(),
}));

vi.mock("../../../api/maintenanceCollectionReminders", async () => {
  const actual = await vi.importActual<
    typeof import("../../../api/maintenanceCollectionReminders")
  >("../../../api/maintenanceCollectionReminders");
  return { ...actual, followUpCollectionMilestone };
});

function buttonRef(ref: RefObject<HTMLElement | null>): Ref<HTMLButtonElement> {
  return ref as unknown as Ref<HTMLButtonElement>;
}

import CollectionMilestoneFollowUpModal from "../CollectionMilestoneFollowUpModal";
import { COLLECTION_FOLLOW_UP } from "../maintenanceLanguage";
import type { CollectionMilestoneRow } from "../../../api/maintenanceCollectionReminders";

const milestone = (overrides: Partial<CollectionMilestoneRow> = {}): CollectionMilestoneRow => ({
  milestone_id: "milestone-1",
  project_contract_id: "pc-1",
  contract_no: "HT-001",
  sequence: 2,
  planned_date: "2026-08-01",
  date_precision: "month",
  planned_month: "2026-08",
  planned_amount: "1234.50",
  completeness_state: "complete",
  follow_up_status: "pending",
  reminder_state: "due_this_month",
  follow_up_review_required: false,
  followed_up_by: null,
  followed_up_at: null,
  follow_up_note: null,
  last_operation: null,
  version: 2,
  ...overrides,
});

interface HarnessProps {
  open?: boolean;
  action?: "handle" | "reschedule" | "reopen";
  row?: CollectionMilestoneRow;
  onSubmitted?: () => void;
  onClose?: () => void;
}

function renderModal({
  open = true,
  action = "handle",
  row = milestone(),
  onSubmitted = vi.fn(),
  onClose = vi.fn(),
}: HarnessProps = {}) {
  const triggerRef: RefObject<HTMLElement | null> = createRef();
  const utils = render(
    <>
      <button type="button" ref={buttonRef(triggerRef)}>触发按钮</button>
      <CollectionMilestoneFollowUpModal
        open={open}
        milestone={row}
        action={action}
        triggerRef={triggerRef}
        onClose={onClose}
        onSubmitted={onSubmitted}
      />
    </>,
  );
  return { triggerRef, onSubmitted, onClose, ...utils };
}

beforeEach(() => {
  vi.clearAllMocks();
  followUpCollectionMilestone.mockResolvedValue({ data: { row: milestone(), data_version: "v2", idempotent_replay: false } });
});

afterEach(() => {
  cleanup();
});

describe("CollectionMilestoneFollowUpModal", () => {
  it("handle 备注可选：不填可提交，填写则随请求发送", async () => {
    const { onSubmitted } = renderModal({ action: "handle", row: milestone() });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_FOLLOW_UP.submit }));
    await waitFor(() => expect(followUpCollectionMilestone).toHaveBeenCalledTimes(1));
    const body = followUpCollectionMilestone.mock.calls[0][1];
    expect(body).toMatchObject({
      expected_version: 2,
      action: "handle",
      note: null,
    });
    expect(body.idempotency_key).toMatch(/^handle-/);
    expect(body).not.toHaveProperty("planned_month");
    expect(body).not.toHaveProperty("reason");

    fireEvent.change(screen.getByLabelText(COLLECTION_FOLLOW_UP.noteLabel), {
      target: { value: "已电话跟进" },
    });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_FOLLOW_UP.submit }));
    await waitFor(() => expect(followUpCollectionMilestone).toHaveBeenCalledTimes(2));
    expect(followUpCollectionMilestone.mock.calls[1][1]).toMatchObject({
      action: "handle",
      note: "已电话跟进",
    });
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledTimes(2));
  });

  it("reschedule 月份和理由必填，缺少任一都不发请求", async () => {
    const { onSubmitted } = renderModal({ action: "reschedule", row: milestone() });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_FOLLOW_UP.submit }));
    expect(await screen.findByText(COLLECTION_FOLLOW_UP.monthRequired)).toBeInTheDocument();
    expect(followUpCollectionMilestone).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(COLLECTION_FOLLOW_UP.plannedMonthLabel), {
      target: { value: "2026-10" },
    });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_FOLLOW_UP.submit }));
    expect(await screen.findByText(COLLECTION_FOLLOW_UP.reasonRequired)).toBeInTheDocument();
    expect(followUpCollectionMilestone).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(COLLECTION_FOLLOW_UP.reasonLabel), {
      target: { value: "客户变更验收时间" },
    });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_FOLLOW_UP.submit }));
    await waitFor(() => expect(followUpCollectionMilestone).toHaveBeenCalledTimes(1));
    const body = followUpCollectionMilestone.mock.calls[0][1];
    expect(body).toMatchObject({
      expected_version: 2,
      action: "reschedule",
      planned_month: "2026-10",
      reason: "客户变更验收时间",
    });
    expect(body).not.toHaveProperty("note");
    expect(onSubmitted).toHaveBeenCalledTimes(1);
  });

  it("reopen 理由必填；请求只含 action 相关字段", async () => {
    renderModal({ action: "reopen", row: milestone({ follow_up_status: "handled", reminder_state: "handled" }) });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_FOLLOW_UP.submit }));
    expect(await screen.findByText(COLLECTION_FOLLOW_UP.reasonRequired)).toBeInTheDocument();
    expect(followUpCollectionMilestone).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(COLLECTION_FOLLOW_UP.reasonLabel), {
      target: { value: "误处理，重新进入提醒队列" },
    });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_FOLLOW_UP.submit }));
    await waitFor(() => expect(followUpCollectionMilestone).toHaveBeenCalledTimes(1));
    const body = followUpCollectionMilestone.mock.calls[0][1];
    expect(body).toMatchObject({
      expected_version: 2,
      action: "reopen",
      reason: "误处理，重新进入提醒队列",
    });
    expect(body).not.toHaveProperty("planned_month");
    expect(body).not.toHaveProperty("note");
  });

  it("首次提交生成幂等键，网络重试复用同一键，表单变化才生成新键", async () => {
    followUpCollectionMilestone.mockRejectedValueOnce(new Error("network down"));
    const { onSubmitted } = renderModal({ action: "handle", row: milestone() });

    fireEvent.click(screen.getByRole("button", { name: COLLECTION_FOLLOW_UP.submit }));
    await waitFor(() => expect(followUpCollectionMilestone).toHaveBeenCalledTimes(1));
    const firstKey = followUpCollectionMilestone.mock.calls[0][1].idempotency_key;

    // 同一次表单的网络重试：键不变
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_FOLLOW_UP.submit }));
    await waitFor(() => expect(followUpCollectionMilestone).toHaveBeenCalledTimes(2));
    expect(followUpCollectionMilestone.mock.calls[1][1].idempotency_key).toBe(firstKey);

    // 表单变化：生成新键
    fireEvent.change(screen.getByLabelText(COLLECTION_FOLLOW_UP.noteLabel), {
      target: { value: "补充说明" },
    });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_FOLLOW_UP.submit }));
    await waitFor(() => expect(followUpCollectionMilestone).toHaveBeenCalledTimes(3));
    const secondKey = followUpCollectionMilestone.mock.calls[2][1].idempotency_key;
    expect(secondKey).not.toBe(firstKey);
    expect(secondKey).toMatch(/^handle-/);

    // 表单未再变化时继续复用第二个键
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledTimes(2));
    const retryButton = await waitFor(() => {
      const button = screen.getByRole("button", { name: /确认$/ });
      expect(button).toBeEnabled();
      expect(button).not.toHaveClass("ant-btn-loading");
      return button;
    });
    fireEvent.click(retryButton);
    await waitFor(() => expect(followUpCollectionMilestone).toHaveBeenCalledTimes(4));
    expect(followUpCollectionMilestone.mock.calls[3][1].idempotency_key).toBe(secondKey);
    expect(onSubmitted).toHaveBeenCalledTimes(2);
  });

  it("409 冲突显示刷新提示且不关闭弹窗", async () => {
    const conflict = Object.assign(new Error("conflict"), {
      response: { status: 409, data: { detail: { code: "version_conflict", message: "数据已变化，请刷新后重试" } } },
    });
    followUpCollectionMilestone.mockRejectedValueOnce(conflict);
    const { onSubmitted, onClose } = renderModal({ action: "handle", row: milestone() });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_FOLLOW_UP.submit }));
    expect(await screen.findByText("数据已变化，请刷新后重试")).toBeInTheDocument();
    expect(onSubmitted).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("打开后焦点进入标题，关闭后回到触发按钮", async () => {
    const { triggerRef } = renderModal({ action: "reopen", row: milestone() });
    const title = screen.getByText(COLLECTION_FOLLOW_UP.reopenTitle);
    await waitFor(() => expect(title).toHaveFocus());

    fireEvent.click(screen.getByRole("button", { name: COLLECTION_FOLLOW_UP.cancel }));
    await waitFor(() => expect(triggerRef.current).toHaveFocus());
  });

  it("关闭后再打开时重置表单与幂等键", async () => {
    const { rerender, triggerRef } = renderModal({ action: "handle", row: milestone() });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_FOLLOW_UP.submit }));
    await waitFor(() => expect(followUpCollectionMilestone).toHaveBeenCalledTimes(1));
    const firstKey = followUpCollectionMilestone.mock.calls[0][1].idempotency_key;

    const onClose = vi.fn();
    rerender(
      <>
        <button type="button" ref={buttonRef(triggerRef)}>触发按钮</button>
        <CollectionMilestoneFollowUpModal
          open={false}
          milestone={null}
          action={null}
          triggerRef={triggerRef}
          onClose={onClose}
          onSubmitted={vi.fn()}
        />
      </>,
    );
    rerender(
      <>
        <button type="button" ref={buttonRef(triggerRef)}>触发按钮</button>
        <CollectionMilestoneFollowUpModal
          open
          milestone={milestone()}
          action="handle"
          triggerRef={triggerRef}
          onClose={onClose}
          onSubmitted={vi.fn()}
        />
      </>,
    );
    const note = screen.getByLabelText(COLLECTION_FOLLOW_UP.noteLabel);
    fireEvent.change(note, { target: { value: "第二次跟进" } });
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_FOLLOW_UP.submit }));
    await waitFor(() => expect(followUpCollectionMilestone).toHaveBeenCalledTimes(2));
    expect(followUpCollectionMilestone.mock.calls[1][1].idempotency_key).not.toBe(firstKey);
  });

  it("弹窗内不出现到账口径文案", () => {
    renderModal({ action: "handle", row: milestone() });
    const dialog = document.querySelector(".ant-modal-content") as HTMLElement;
    const text = dialog.textContent ?? "";
    for (const term of ["已到账", "实收", "待收", "回款率", "到账率", "凭证", "核销"]) {
      expect(text).not.toContain(term);
    }
  });

  it("弹窗内表单控件可无障碍定位", () => {
    const { container } = renderModal({ action: "reschedule", row: milestone() });
    expect(within(container as HTMLElement).getByLabelText(COLLECTION_FOLLOW_UP.plannedMonthLabel)).toBeInTheDocument();
    expect(within(container as HTMLElement).getByLabelText(COLLECTION_FOLLOW_UP.reasonLabel)).toBeInTheDocument();
  });
});
