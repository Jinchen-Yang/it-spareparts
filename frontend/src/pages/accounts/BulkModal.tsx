/** 批量权限操作向导：配置 → dry-run 预览（影响账号数+逐键差异）→ 指纹执行 → 结果。
 * 全成或全败：400 逐账号原因整体拒绝；409 = 预览后被人改过，提示重新预览。 */
import { useMemo, useState } from "react";
import { Alert, Button, Modal, Select, Space, Steps, Table, Tag, message } from "antd";
import type { Account, AccountsMeta, BulkOperation, BulkPreview, BulkPreviewItem } from "../../api/accounts";
import { bulkAccounts, explainApiError } from "../../api/accounts";

const OP_LABEL: Record<BulkOperation, string> = {
  apply_template: "套用职位模板",
  grant: "增加权限",
  revoke: "取消权限",
  reset_to_template: "恢复模板默认值",
};

export default function BulkModal({ meta, operation, targets, onClose, onDone }: {
  meta: AccountsMeta;
  operation: BulkOperation | null;
  targets: Account[];
  onClose: () => void;
  onDone: () => void;
}) {
  const [step, setStep] = useState(0);
  const [tplCode, setTplCode] = useState<string>();
  const [keys, setKeys] = useState<string[]>([]);
  const [preview, setPreview] = useState<BulkPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ applied: number } | null>(null);

  const usernames = targets.map((t) => t.username);
  const needConfig = operation === "apply_template" || operation === "grant" || operation === "revoke";
  const keyOptions = useMemo(() => meta.groups.flatMap((g) => g.keys.map((k) => ({
    value: k,
    label: `${g.label} · ${meta.meta[k]?.label || meta.labels[k] || k}`,
  }))), [meta]);

  const reset = () => {
    setStep(0); setTplCode(undefined); setKeys([]); setPreview(null);
    setError(null); setResult(null);
  };

  const runPreview = async () => {
    if (!operation) return;
    setBusy(true); setError(null);
    try {
      const r = await bulkAccounts({
        usernames, operation, dry_run: true,
        template_code: tplCode, keys: keys.length ? keys : undefined,
      });
      setPreview(r.data as BulkPreview);
      setStep(1);
    } catch (e) {
      setError(explainApiError(e, "预览失败"));
    } finally {
      setBusy(false);
    }
  };

  const execute = async () => {
    if (!operation || !preview) return;
    setBusy(true); setError(null);
    try {
      const r = await bulkAccounts({
        usernames, operation, dry_run: false, fingerprint: preview.fingerprint,
        template_code: tplCode, keys: keys.length ? keys : undefined,
      });
      setResult(r.data as { applied: number });
      setStep(2);
      onDone();
    } catch (e) {
      const status = (e as { response?: { status?: number } })?.response?.status;
      if (status === 409) {
        setError(explainApiError(e, "预览后账号被他人修改，请重新预览"));
        setPreview(null);
        setStep(0);
      } else {
        setError(explainApiError(e, "执行失败（未做任何修改）"));
      }
    } finally {
      setBusy(false);
    }
  };

  const previewColumns = [
    { title: "账号", dataIndex: "username",
      render: (v: string, r: BulkPreviewItem) => <span>{r.display_name || v} <span style={{ color: "#999" }}>({v})</span></span> },
    { title: "权限变化", dataIndex: "changed_keys",
      render: (cks: BulkPreviewItem["changed_keys"], r: BulkPreviewItem) => (
        <Space size={4} wrap>
          {r.template_before !== r.template_after && (
            <Tag color="purple">模板 {r.template_before || "—"} → {r.template_after}</Tag>
          )}
          {cks.length === 0 && r.template_before === r.template_after
            ? <span style={{ color: "#999" }}>无变化</span>
            : cks.map((c) => (
              <Tag key={c.key} color={c.to ? "green" : "orange"}>
                {c.label}：{c.from ? "开" : "关"} → {c.to ? "开" : "关"}
              </Tag>
            ))}
        </Space>
      ) },
  ];

  return (
    <Modal
      title={operation ? `批量操作 · ${OP_LABEL[operation]}（${targets.length} 个账号）` : ""}
      open={!!operation}
      onCancel={() => { reset(); onClose(); }}
      width={760}
      footer={null}
      destroyOnClose
    >
      <Steps
        size="small"
        current={step}
        items={[{ title: "配置" }, { title: "预览确认" }, { title: "完成" }]}
        style={{ marginBottom: 16 }}
      />
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}

      {step === 0 && (
        <div>
          {operation === "apply_template" && (
            <>
              <div style={{ marginBottom: 8, fontSize: 13, color: "#666" }}>
                套用后这些账号的权限与角色都回到模板口径，原有个别调整会被清除。
              </div>
              <Select
                style={{ width: "100%", marginBottom: 12 }}
                placeholder="选择职位模板"
                value={tplCode}
                onChange={setTplCode}
                options={meta.templates.filter((t) => t.is_active && !t.locked).map((t) => ({
                  value: t.code, label: `${t.name}（${t.description || t.base_role}）`,
                }))}
              />
            </>
          )}
          {(operation === "grant" || operation === "revoke") && (
            <>
              <div style={{ marginBottom: 8, fontSize: 13, color: "#666" }}>
                {operation === "grant"
                  ? "为所有选中账号开启这些权限（缺依赖会整体拒绝，请把依赖一并勾上）。"
                  : "为所有选中账号关闭这些权限（若有已开启的动作依赖它们，会整体拒绝）。"}
              </div>
              <Select
                mode="multiple"
                style={{ width: "100%", marginBottom: 12 }}
                placeholder="选择权限项（可多选、可搜索）"
                value={keys}
                onChange={setKeys}
                optionFilterProp="label"
                options={keyOptions}
              />
            </>
          )}
          {operation === "reset_to_template" && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message="清除这些账号的全部个别调整，回到各自模板快照的默认值（不会隐式升级模板版本）。"
            />
          )}
          <Space style={{ display: "flex", justifyContent: "flex-end" }}>
            <Button onClick={() => { reset(); onClose(); }}>取消</Button>
            <Button
              type="primary"
              loading={busy}
              disabled={needConfig && operation === "apply_template" ? !tplCode
                : (operation === "grant" || operation === "revoke") ? keys.length === 0 : false}
              onClick={runPreview}
            >预览影响</Button>
          </Space>
        </div>
      )}

      {step === 1 && preview && (
        <div>
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={`将影响 ${preview.affected} 个账号（${preview.changed} 个有实际变化）——执行后这些账号的旧登录立即失效`}
            description="全成功或全失败：任何一个账号不满足条件，整批都不会执行。"
          />
          <Table
            size="small"
            rowKey="username"
            columns={previewColumns as never}
            dataSource={preview.preview}
            pagination={preview.preview.length > 10 ? { pageSize: 10 } : false}
            scroll={{ x: 480 }}
          />
          <Space style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
            <Button onClick={() => setStep(0)}>返回修改</Button>
            <Button type="primary" danger loading={busy} onClick={execute}>确认执行</Button>
          </Space>
        </div>
      )}

      {step === 2 && result && (
        <div>
          <Alert
            type="success"
            showIcon
            message={`已成功更新 ${result.applied} 个账号`}
            description="相关账号的旧登录已失效，重新登录后取到新权限。操作已写入审计日志。"
            style={{ marginBottom: 12 }}
          />
          <Space style={{ display: "flex", justifyContent: "flex-end" }}>
            <Button type="primary" onClick={() => { reset(); onClose(); }}>完成</Button>
          </Space>
        </div>
      )}
    </Modal>
  );
}

export function bulkOpLabel(op: BulkOperation): string {
  return OP_LABEL[op];
}
