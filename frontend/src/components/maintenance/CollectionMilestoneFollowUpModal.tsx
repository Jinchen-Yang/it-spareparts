import { useEffect, useRef, useState } from "react";
import { Alert, ConfigProvider, Input, Modal } from "antd";
import type { RefObject } from "react";

import {
  followUpCollectionMilestone,
  type CollectionFollowUpAction,
  type CollectionFollowUpRequest,
  type CollectionMilestoneRow,
} from "../../api/maintenanceCollectionReminders";
import { COLLECTION_FOLLOW_UP } from "./maintenanceLanguage";

const MONTH_PATTERN = /^\d{4}-\d{2}$/;

/**
 * 首次提交时生成幂等键；同一次表单的网络重试复用同一键，
 * 表单字段变化后才重新生成。
 */
function newIdempotencyKey(action: CollectionFollowUpAction, milestoneId: string) {
  return `${action}-${milestoneId}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

interface CollectionMilestoneFollowUpModalProps {
  open: boolean;
  milestone: CollectionMilestoneRow | null;
  action: CollectionFollowUpAction | null;
  triggerRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  onSubmitted: () => void;
}

function errorMessage(reason: unknown, fallback: string): string {
  const response = (reason as { response?: { data?: { detail?: { message?: string } } } })
    ?.response?.data?.detail?.message;
  return response || fallback;
}

export default function CollectionMilestoneFollowUpModal({
  open,
  milestone,
  action,
  triggerRef,
  onClose,
  onSubmitted,
}: CollectionMilestoneFollowUpModalProps) {
  const [note, setNote] = useState("");
  const [plannedMonth, setPlannedMonth] = useState("");
  const [reason, setReason] = useState("");
  const [monthError, setMonthError] = useState<string | null>(null);
  const [reasonError, setReasonError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const idempotencyKeyRef = useRef<string | null>(null);
  const submittedKeysRef = useRef<Set<string>>(new Set());
  const titleRef = useRef<HTMLHeadingElement | null>(null);
  const titleId = "collection-follow-up-title";

  // 打开时重置表单、幂等键与已通知键，并把焦点送入标题；关闭后焦点回到触发按钮。
  useEffect(() => {
    if (!open) {
      triggerRef.current?.focus();
      return undefined;
    }
    setNote("");
    setPlannedMonth("");
    setReason("");
    setMonthError(null);
    setReasonError(null);
    setSubmitError(null);
    idempotencyKeyRef.current = null;
    submittedKeysRef.current = new Set();
    const timer = window.setTimeout(() => titleRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [open, milestone, triggerRef]);

  const invalidateIdempotencyKey = () => {
    idempotencyKeyRef.current = null;
  };

  const handleSubmit = async () => {
    if (!milestone || !action || submitting) return;
    if (action === "reschedule" && !MONTH_PATTERN.test(plannedMonth.trim())) {
      setMonthError(COLLECTION_FOLLOW_UP.monthRequired);
      return;
    }
    const trimmedReason = reason.trim();
    if (action !== "handle" && !trimmedReason) {
      setReasonError(COLLECTION_FOLLOW_UP.reasonRequired);
      return;
    }
    if (!idempotencyKeyRef.current) {
      idempotencyKeyRef.current = newIdempotencyKey(action, milestone.milestone_id);
    }
    setMonthError(null);
    setReasonError(null);
    setSubmitError(null);
    const request: CollectionFollowUpRequest = {
      expected_version: milestone.version,
      idempotency_key: idempotencyKeyRef.current,
      action,
    };
    if (action === "handle") {
      request.note = note.trim() || null;
    } else if (action === "reschedule") {
      request.planned_month = plannedMonth.trim();
      request.reason = trimmedReason;
    } else {
      request.reason = trimmedReason;
    }
    setSubmitting(true);
    try {
      await followUpCollectionMilestone(milestone.milestone_id, request);
      // 同一幂等键（同一操作重试）成功后只通知父级一次。
      if (!submittedKeysRef.current.has(request.idempotency_key)) {
        submittedKeysRef.current.add(request.idempotency_key);
        onSubmitted();
      }
    } catch (reason_) {
      const status = (reason_ as { response?: { status?: number } })?.response?.status;
      setSubmitError(
        status === 409
          ? errorMessage(reason_, COLLECTION_FOLLOW_UP.versionConflict)
          : errorMessage(reason_, COLLECTION_FOLLOW_UP.submitFailed),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const handleCancel = () => {
    triggerRef.current?.focus();
    onClose();
  };

  const title =
    action === "reschedule"
      ? COLLECTION_FOLLOW_UP.rescheduleTitle
      : action === "reopen"
        ? COLLECTION_FOLLOW_UP.reopenTitle
        : COLLECTION_FOLLOW_UP.handleTitle;

  return (
    <ConfigProvider button={{ autoInsertSpace: false }}>
      <Modal
        open={open}
        closable={false}
        maskClosable={false}
        onCancel={handleCancel}
        onOk={handleSubmit}
        okText={COLLECTION_FOLLOW_UP.submit}
        cancelText={COLLECTION_FOLLOW_UP.cancel}
        confirmLoading={submitting}
        aria-labelledby={titleId}
        getContainer={false}
        width={480}
      >
      <h3
        id={titleId}
        ref={titleRef}
        tabIndex={-1}
        style={{ margin: "0 0 12px", fontSize: 16, fontWeight: 600 }}
      >
        {title}
      </h3>
      {submitError && <Alert type="error" showIcon message={submitError} />}
      {action === "handle" && (
        <div style={{ marginTop: 12 }}>
          <label htmlFor="collection-follow-up-note">{COLLECTION_FOLLOW_UP.noteLabel}</label>
          <Input.TextArea
            id="collection-follow-up-note"
            aria-label={COLLECTION_FOLLOW_UP.noteLabel}
            value={note}
            maxLength={1000}
            rows={2}
            placeholder={COLLECTION_FOLLOW_UP.notePlaceholder}
            onChange={(event) => {
              setNote(event.target.value);
              invalidateIdempotencyKey();
            }}
          />
        </div>
      )}
      {action === "reschedule" && (
        <div style={{ marginTop: 12 }}>
          <label htmlFor="collection-follow-up-month">{COLLECTION_FOLLOW_UP.plannedMonthLabel}</label>
          <Input
            id="collection-follow-up-month"
            aria-label={COLLECTION_FOLLOW_UP.plannedMonthLabel}
            value={plannedMonth}
            maxLength={7}
            placeholder={COLLECTION_FOLLOW_UP.plannedMonthPlaceholder}
            onChange={(event) => {
              setPlannedMonth(event.target.value);
              setMonthError(null);
              invalidateIdempotencyKey();
            }}
          />
          {monthError && <div style={{ color: "#cf1322", fontSize: 13, marginTop: 4 }}>{monthError}</div>}
        </div>
      )}
      {action !== "handle" && (
        <div style={{ marginTop: 12 }}>
          <label htmlFor="collection-follow-up-reason">{COLLECTION_FOLLOW_UP.reasonLabel}</label>
          <Input.TextArea
            id="collection-follow-up-reason"
            aria-label={COLLECTION_FOLLOW_UP.reasonLabel}
            value={reason}
            maxLength={1000}
            rows={2}
            onChange={(event) => {
              setReason(event.target.value);
              setReasonError(null);
              invalidateIdempotencyKey();
            }}
          />
          {reasonError && <div style={{ color: "#cf1322", fontSize: 13, marginTop: 4 }}>{reasonError}</div>}
        </div>
      )}
      </Modal>
    </ConfigProvider>
  );
}
