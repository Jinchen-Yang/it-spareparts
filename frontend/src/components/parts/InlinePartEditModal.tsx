import {
  useAuthorizedRunner,
  useModuleAccess,
  legacyRouteAllowed,
} from "../../authz/access";
import { surfaceRule } from "../../authz/rules";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { App, Alert, Button, Input, Modal, Select, Space } from "antd";
import { type CategoryNode, type MasterFields } from "../../api";
import {
  fetchMasterEditCategories,
  fetchMasterEditState,
  fetchOverview,
  masterEditConflictLines,
  masterEditErrorMessage,
  rebaseMasterEditDraft,
  saveMasterEdit,
  type MasterEditPayload,
  type MasterEditSnapshot,
} from "../../api/search";

type EditablePart = Required<
  Pick<MasterFields, "description" | "category_major" | "category_minor">
> & {
  pn_std: string;
};

interface Props {
  open: boolean;
  canEdit: boolean;
  contextKey: string;
  partId?: number | null;
  pn: string | null;
  onClose: () => void;
  onSaved: () => void | Promise<void>;
}

const normalized = (value: string | null | undefined) => {
  if (typeof value !== "string") return value ?? null;
  return value.trim() || null;
};

/**
 * 型号全景里的轻量主数据编辑器。
 * 只开放甲方本次明确提出的描述与两级品类，不复制完整主数据页的其它能力。
 */
export default function InlinePartEditModal({
  open,
  canEdit,
  contextKey,
  partId,
  pn,
  onClose,
  onSaved,
}: Props) {
  const { message } = App.useApp();
  const access = useModuleAccess("part_master");
  const [oldAllowed] = useState(() => legacyRouteAllowed("/master"));
  const run = useAuthorizedRunner();
  const captured = useRef({
    token: access.token!,
    snapshotVersion: access.snapshot?.version || "",
    generation: access.generation,
  });
  const [form, setForm] = useState<EditablePart | null>(null);
  const [initial, setInitial] = useState<EditablePart | null>(null);
  const [baseline, setBaseline] = useState<MasterEditSnapshot | null>(null);
  const [categories, setCategories] = useState<CategoryNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [conflictLines, setConflictLines] = useState<string[]>([]);
  const [canRebase, setCanRebase] = useState(false);
  const [rebasing, setRebasing] = useState(false);
  const loadSeq = useRef(0);
  const sessionSeq = useRef(0);
  const mountedToken = useRef(localStorage.getItem("token"));
  const sessionToken = useRef<string | null>(null);
  // 独立于父组件再取一次挂载时权限快照：既防父级误传 true，也不让后续
  // localStorage 篡改改变守卫结果。token 变化会由 App 重挂整棵页面树。
  const hasSessionPermission = access.surface("inline_edit", oldAllowed);
  const canEditNow = () =>
    canEdit && hasSessionPermission && access.isCurrent(captured.current);
  const permittedOpen = open && canEditNow();

  useLayoutEffect(() => {
    sessionSeq.current += 1;
    ++loadSeq.current;
    sessionToken.current = permittedOpen ? mountedToken.current : null;
    setSaving(false);
    setRebasing(false);
    setCanRebase(false);
    setSaveError(null);
    setConflictLines([]);
    return () => {
      sessionSeq.current += 1;
      ++loadSeq.current;
    };
  }, [
    permittedOpen,
    partId,
    pn,
    contextKey,
    access.snapshot?.version,
    access.generation,
  ]);

  const load = async () => {
    if (
      !canEditNow() ||
      !sessionToken.current ||
      localStorage.getItem("token") !== sessionToken.current ||
      !pn
    )
      return;
    const seq = ++loadSeq.current;
    const session = sessionSeq.current;
    const authToken = sessionToken.current;
    const isCurrent = () =>
      canEditNow() &&
      seq === loadSeq.current &&
      session === sessionSeq.current &&
      localStorage.getItem("token") === authToken;
    setLoading(true);
    setLoadError(null);
    setSaveError(null);
    setConflictLines([]);
    setCanRebase(false);
    setForm(null);
    setInitial(null);
    setBaseline(null);
    try {
      const [snapshot, categoryResp] = await Promise.all([
        (async () => {
          let stableId = partId;
          // 兼容只携 PN 的旧调用者也不能把合并/别名重定向当作编辑目标。
          if (stableId == null) {
            if (!access.legacy)
              throw new Error("缺少稳定型号 ID，请重新打开编辑");
            const overview = await fetchOverview({ pn_std: pn }, authToken);
            if (!isCurrent()) return null;
            if (!overview.part || overview.part.pn_std !== pn) {
              throw new Error("型号身份已变化，请关闭后重新打开编辑");
            }
            stableId = overview.part.id;
          }
          const state = await run({
            rule: surfaceRule("part_master.inline_edit"),
            legacy: { part_master: oldAllowed },
            request: (capture) =>
              fetchMasterEditState(stableId!, capture.token, capture.signal),
          });
          if (
            state.part_id !== stableId ||
            state.pn_std !== pn ||
            state.identity.status !== "active"
          ) {
            throw new Error("型号身份已变化，请关闭后重新打开编辑");
          }
          return state;
        })(),
        run({
          rule: surfaceRule("part_master.inline_edit"),
          legacy: { part_master: oldAllowed },
          request: (capture) =>
            fetchMasterEditCategories(capture.token, capture.signal),
        }),
      ]);
      if (!isCurrent() || !snapshot) return;
      const next: EditablePart = {
        pn_std: snapshot.pn_std,
        description: snapshot.fields.description,
        category_major: snapshot.fields.category_major,
        category_minor: snapshot.fields.category_minor,
      };
      setBaseline(snapshot);
      setForm(next);
      setInitial(next);
      setCategories(categoryResp);
    } catch (error: any) {
      if (!isCurrent()) return;
      setLoadError(
        masterEditErrorMessage(
          error,
          error?.message || "型号信息加载失败，请重试",
        ),
      );
    } finally {
      if (isCurrent()) setLoading(false);
    }
  };

  useEffect(() => {
    if (permittedOpen && pn) load();
    else {
      ++loadSeq.current;
      setForm(null);
      setInitial(null);
      setBaseline(null);
      setLoadError(null);
      setSaveError(null);
    }
    // pn 变化必须重新加载；load 只使用当前 pn。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    permittedOpen,
    partId,
    pn,
    contextKey,
    access.snapshot?.version,
    access.generation,
  ]);

  const minorOptions = (
    categories.find((c) => c.name === form?.category_major)?.children || []
  ).map((child) => ({ label: child.name, value: child.name }));

  const save = async () => {
    const authToken = sessionToken.current;
    if (
      !canEditNow() ||
      !authToken ||
      localStorage.getItem("token") !== authToken ||
      !form ||
      !initial ||
      !baseline ||
      saving ||
      rebasing
    )
      return;
    const session = sessionSeq.current;
    const isCurrentSession = () =>
      session === sessionSeq.current &&
      canEditNow() &&
      localStorage.getItem("token") === authToken;
    const payload: MasterEditPayload = {
      part_id: baseline.part_id,
      pn_std: baseline.pn_std,
      base_snapshot: baseline,
    };
    (["description", "category_major", "category_minor"] as const).forEach(
      (key) => {
        const before = normalized(initial[key]);
        const after = normalized(form[key]);
        if (before !== after) payload[key] = after;
      },
    );
    if (Object.keys(payload).length === 3) {
      message.info("没有改动");
      return;
    }

    setSaving(true);
    setSaveError(null);
    setConflictLines([]);
    setCanRebase(false);
    try {
      const { data } = await run({
        rule: surfaceRule("part_master.inline_edit"),
        legacy: { part_master: oldAllowed },
        request: (capture) =>
          saveMasterEdit(payload, capture.token, capture.signal),
      });
      if (!isCurrentSession()) return;
      message.success(`已保存（${data.updated.join("、")} 将不会被重导覆盖）`);
      onClose();
      // 写入已经成功后，刷新失败不能再误报为“保存失败”，避免管理员重复提交。
      try {
        await onSaved();
      } catch {
        if (isCurrentSession()) {
          message.warning("修改已保存，但当前页面刷新失败，请稍后手动刷新");
        }
      }
    } catch (error: any) {
      if (isCurrentSession()) {
        setSaveError(masterEditErrorMessage(error, "保存失败，请重试"));
        setConflictLines(masterEditConflictLines(error));
        setCanRebase(
          error?.response?.status === 409 &&
            error?.response?.data?.detail?.code === "field_conflict",
        );
      }
    } finally {
      if (isCurrentSession()) setSaving(false);
    }
  };

  const rebaseDraft = async () => {
    const authToken = sessionToken.current;
    if (
      !canRebase ||
      !canEditNow() ||
      !authToken ||
      !form ||
      !baseline ||
      saving ||
      rebasing ||
      localStorage.getItem("token") !== authToken
    )
      return;
    const session = sessionSeq.current;
    const seq = ++loadSeq.current;
    const isCurrent = () =>
      canEditNow() &&
      seq === loadSeq.current &&
      session === sessionSeq.current &&
      canEditNow() &&
      localStorage.getItem("token") === authToken;
    setRebasing(true);
    try {
      const latest = await run({
        rule: surfaceRule("part_master.inline_edit"),
        legacy: { part_master: oldAllowed },
        request: (capture) =>
          fetchMasterEditState(baseline.part_id, capture.token, capture.signal),
      });
      if (!isCurrent()) return;
      const next = rebaseMasterEditDraft(baseline, latest, form);
      setBaseline(latest);
      setInitial({ pn_std: latest.pn_std, ...latest.fields });
      setForm({ pn_std: latest.pn_std, ...next });
      setSaveError(null);
      setConflictLines([]);
      setCanRebase(false);
      message.info("已更新编辑基线并保留草稿，请核对后再次保存");
    } catch (error: any) {
      if (isCurrent()) {
        setSaveError(
          masterEditErrorMessage(
            error,
            error?.message || "基线加载失败，请重试",
          ),
        );
      }
    } finally {
      if (isCurrent()) setRebasing(false);
    }
  };

  return (
    <Modal
      open={permittedOpen}
      title={pn ? `就地编辑备件 ${pn}` : "就地编辑备件"}
      onCancel={onClose}
      onOk={save}
      okText="保存"
      cancelText="取消"
      confirmLoading={saving}
      okButtonProps={{
        disabled:
          !canEdit || loading || rebasing || !form || Boolean(loadError),
      }}
      width={560}
    >
      <Space direction="vertical" size={14} style={{ width: "100%" }}>
        <Alert
          type="info"
          showIcon
          message="人工修改会记录操作人；保存后的字段不会被氚云重导覆盖。"
        />
        {saveError && (
          <Alert
            type="error"
            showIcon
            message={saveError}
            description={
              <div style={{ overflowWrap: "anywhere" }}>
                {conflictLines.map((line, index) => (
                  <div key={index} style={{ whiteSpace: "pre-wrap" }}>
                    {line}
                  </div>
                ))}
                <div>草稿已保留；请核对当前值与本次修改。</div>
                {canRebase && (
                  <Button
                    loading={rebasing}
                    disabled={saving}
                    onClick={rebaseDraft}
                    style={{
                      height: "auto",
                      whiteSpace: "normal",
                      marginTop: 8,
                      maxWidth: "100%",
                    }}
                  >
                    以当前值为新基线并保留我的草稿
                  </Button>
                )}
              </div>
            }
          />
        )}
        {loadError ? (
          <Alert
            type="error"
            showIcon
            message={loadError}
            action={
              <Button type="link" size="small" onClick={load}>
                重试
              </Button>
            }
          />
        ) : (
          <>
            <div>
              <label
                htmlFor="inline-part-pn"
                style={{ display: "block", marginBottom: 4 }}
              >
                PN
              </label>
              <Input
                id="inline-part-pn"
                value={form?.pn_std || pn || ""}
                disabled
              />
            </div>
            <div>
              <label
                htmlFor="inline-part-description"
                style={{ display: "block", marginBottom: 4 }}
              >
                描述
              </label>
              <Input.TextArea
                id="inline-part-description"
                value={form?.description ?? ""}
                rows={3}
                disabled={loading || saving || rebasing || !form}
                placeholder="输入便于采购、销售和库房识别的备件描述"
                onChange={(event) =>
                  setForm((current) =>
                    current
                      ? { ...current, description: event.target.value }
                      : current,
                  )
                }
              />
            </div>
            <Space size={12} wrap style={{ width: "100%" }}>
              <div style={{ minWidth: 220, flex: 1 }}>
                <label
                  htmlFor="inline-part-major"
                  style={{ display: "block", marginBottom: 4 }}
                >
                  一级品类
                </label>
                <Select
                  id="inline-part-major"
                  style={{ width: "100%" }}
                  allowClear
                  showSearch
                  loading={loading}
                  disabled={loading || saving || rebasing || !form}
                  value={form?.category_major ?? undefined}
                  placeholder="选择一级品类"
                  options={categories.map((c) => ({
                    label: c.name,
                    value: c.name,
                  }))}
                  onChange={(value) =>
                    setForm((current) =>
                      current
                        ? {
                            ...current,
                            category_major: value ?? null,
                            category_minor: null,
                          }
                        : current,
                    )
                  }
                />
              </div>
              <div style={{ minWidth: 220, flex: 1 }}>
                <label
                  htmlFor="inline-part-minor"
                  style={{ display: "block", marginBottom: 4 }}
                >
                  二级品类
                </label>
                <Select
                  id="inline-part-minor"
                  style={{ width: "100%" }}
                  allowClear
                  showSearch
                  disabled={
                    loading || saving || rebasing || !form?.category_major
                  }
                  value={form?.category_minor ?? undefined}
                  placeholder="选择二级品类"
                  options={minorOptions}
                  onChange={(value) =>
                    setForm((current) =>
                      current
                        ? { ...current, category_minor: value ?? null }
                        : current,
                    )
                  }
                />
              </div>
            </Space>
          </>
        )}
      </Space>
    </Modal>
  );
}
