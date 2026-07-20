import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Alert, Input, Modal, Select, Space, message } from "antd";
import { masterCategories, masterEdit, type CategoryNode, type MasterFields } from "../../api";
import { fetchOverview } from "../../api/search";

type EditablePart = Required<Pick<MasterFields, "description" | "category_major" | "category_minor">> & {
  pn_std: string;
};

interface Props {
  open: boolean;
  canEdit: boolean;
  contextKey: string;
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
  open, canEdit, contextKey, pn, onClose, onSaved,
}: Props) {
  const [form, setForm] = useState<EditablePart | null>(null);
  const [initial, setInitial] = useState<EditablePart | null>(null);
  const [categories, setCategories] = useState<CategoryNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const loadSeq = useRef(0);
  const sessionSeq = useRef(0);
  const sessionToken = useRef<string | null>(null);
  const canEditNow = () => canEdit && localStorage.getItem("role") === "admin";
  const permittedOpen = open && canEditNow();

  useLayoutEffect(() => {
    sessionSeq.current += 1;
    sessionToken.current = permittedOpen ? localStorage.getItem("token") : null;
    setSaving(false);
    return () => { sessionSeq.current += 1; };
  }, [permittedOpen, pn, contextKey]);

  const load = async () => {
    if (!canEditNow() || !sessionToken.current
      || localStorage.getItem("token") !== sessionToken.current || !pn) return;
    const seq = ++loadSeq.current;
    setLoading(true);
    setLoadError(null);
    setForm(null);
    try {
      const [overview, categoryResp] = await Promise.all([
        fetchOverview({ pn_std: pn }),
        masterCategories(),
      ]);
      if (seq !== loadSeq.current) return;
      const next: EditablePart = {
        pn_std: overview.part.pn_std,
        description: overview.part.description ?? null,
        category_major: overview.part.category_major ?? null,
        category_minor: overview.part.category_minor ?? null,
      };
      setForm(next);
      setInitial(next);
      setCategories(categoryResp.data.categories || []);
    } catch (error: any) {
      if (seq !== loadSeq.current) return;
      setLoadError(error?.response?.data?.detail || "型号信息加载失败，请重试");
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  };

  useEffect(() => {
    if (permittedOpen && pn) load();
    else {
      ++loadSeq.current;
      setForm(null);
      setInitial(null);
      setLoadError(null);
    }
    // pn 变化必须重新加载；load 只使用当前 pn。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [permittedOpen, pn]);

  const minorOptions = (categories.find((c) => c.name === form?.category_major)?.children || [])
    .map((child) => ({ label: child.name, value: child.name }));

  const save = async () => {
    const authToken = sessionToken.current;
    if (!canEditNow() || !authToken || localStorage.getItem("token") !== authToken
      || !form || !initial) return;
    const session = sessionSeq.current;
    const isCurrentSession = () => session === sessionSeq.current && canEditNow()
      && localStorage.getItem("token") === authToken;
    const payload: MasterFields & { pn_std: string } = { pn_std: form.pn_std };
    (["description", "category_major", "category_minor"] as const).forEach((key) => {
      const before = normalized(initial[key]);
      const after = normalized(form[key]);
      if (before !== after) payload[key] = after;
    });
    if (Object.keys(payload).length === 1) {
      message.info("没有改动");
      return;
    }

    setSaving(true);
    try {
      const { data } = await masterEdit(payload, authToken);
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
        message.error(error?.response?.data?.detail || "保存失败，请重试");
      }
    } finally {
      if (isCurrentSession()) setSaving(false);
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
      okButtonProps={{ disabled: !canEdit || loading || !form || Boolean(loadError) }}
      width={560}
    >
      <Space direction="vertical" size={14} style={{ width: "100%" }}>
        <Alert
          type="info"
          showIcon
          message="人工修改会记录操作人；保存后的字段不会被氚云重导覆盖。"
        />
        {loadError ? (
          <Alert
            type="error"
            showIcon
            message={loadError}
            action={<a onClick={load}>重试</a>}
          />
        ) : (
          <>
            <div>
              <label htmlFor="inline-part-pn" style={{ display: "block", marginBottom: 4 }}>PN</label>
              <Input id="inline-part-pn" value={form?.pn_std || pn || ""} disabled />
            </div>
            <div>
              <label htmlFor="inline-part-description" style={{ display: "block", marginBottom: 4 }}>描述</label>
              <Input.TextArea
                id="inline-part-description"
                value={form?.description ?? ""}
                rows={3}
                disabled={loading || !form}
                placeholder="输入便于采购、销售和库房识别的备件描述"
                onChange={(event) => setForm((current) => current
                  ? { ...current, description: event.target.value }
                  : current)}
              />
            </div>
            <Space size={12} wrap style={{ width: "100%" }}>
              <div style={{ minWidth: 220, flex: 1 }}>
                <label htmlFor="inline-part-major" style={{ display: "block", marginBottom: 4 }}>一级品类</label>
                <Select
                  id="inline-part-major"
                  style={{ width: "100%" }}
                  allowClear
                  showSearch
                  loading={loading}
                  disabled={loading || !form}
                  value={form?.category_major ?? undefined}
                  placeholder="选择一级品类"
                  options={categories.map((c) => ({ label: c.name, value: c.name }))}
                  onChange={(value) => setForm((current) => current
                    ? { ...current, category_major: value ?? null, category_minor: null }
                    : current)}
                />
              </div>
              <div style={{ minWidth: 220, flex: 1 }}>
                <label htmlFor="inline-part-minor" style={{ display: "block", marginBottom: 4 }}>二级品类</label>
                <Select
                  id="inline-part-minor"
                  style={{ width: "100%" }}
                  allowClear
                  showSearch
                  disabled={loading || !form?.category_major}
                  value={form?.category_minor ?? undefined}
                  placeholder="选择二级品类"
                  options={minorOptions}
                  onChange={(value) => setForm((current) => current
                    ? { ...current, category_minor: value ?? null }
                    : current)}
                />
              </div>
            </Space>
          </>
        )}
      </Space>
    </Modal>
  );
}
