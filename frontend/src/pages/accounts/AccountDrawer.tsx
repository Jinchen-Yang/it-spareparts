/** 单账号权限抽屉：模板选择 + 权限矩阵（来源标记/模板对比）+ 保存。
 * 保存语义：PUT {template_code?, overrides}——overrides = 矩阵最终图相对模板快照的稀疏 diff。
 * 换模板 = 立即以新模板快照为底座重画矩阵（个别调整清零，由管理员重新按需勾选）。 */
import { useMemo, useState } from "react";
import { Alert, Button, Drawer, Select, Space, Tag, message } from "antd";
import type { Account, AccountsMeta, Perms } from "../../api/accounts";
import { diffOverrides, explainApiError, updateAccount } from "../../api/accounts";
import PermissionMatrix from "./PermissionMatrix";

export default function AccountDrawer({ meta, account, onClose, onSaved }: {
  meta: AccountsMeta;
  account: Account | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [tplCode, setTplCode] = useState<string | null>(null);
  const [perms, setPerms] = useState<Perms | null>(null);
  const [compareOn, setCompareOn] = useState(false);
  const [saving, setSaving] = useState(false);

  // 抽屉每次打开以账号现状初始化（受控于 account 变化）
  const cur = account;
  const effCode = tplCode ?? cur?.template_code ?? null;
  const tpl = meta.templates.find((t) => t.code === effCode);
  const base: Perms = useMemo(() => {
    if (tplCode && tpl) return tpl.permissions;          // 换了模板 → 新快照做底座
    return cur?.template_perms || {};
  }, [tplCode, tpl, cur]);
  const value: Perms = perms ?? cur?.permissions ?? {};

  const choices = meta.templates.filter((t) => t.is_active && !t.locked);
  const dirty = perms !== null || tplCode !== null;

  const save = async () => {
    if (!cur) return;
    setSaving(true);
    try {
      const overrides = diffOverrides(base, value, meta.all_keys);
      await updateAccount(cur.username, tplCode
        ? { template_code: tplCode, overrides }
        : { overrides });
      message.success("权限已保存，该用户旧登录已失效，重新登录后生效");
      onSaved();
      close();
    } catch (e) {
      message.error(explainApiError(e, "保存失败"));
    } finally {
      setSaving(false);
    }
  };

  const close = () => {
    setTplCode(null);
    setPerms(null);
    setCompareOn(false);
    onClose();
  };

  return (
    <Drawer
      title={cur ? `权限设置 · ${cur.display_name || cur.username}` : ""}
      width={720}
      open={!!cur}
      onClose={close}
      styles={{ body: { paddingTop: 12 } }}
      extra={
        <Button type="primary" loading={saving} disabled={!dirty} onClick={save}>保存权限</Button>
      }
    >
      {cur && (
        <>
          <div style={{ marginBottom: 8, fontSize: 13, color: "#888" }}>
            职位模板（账号权限的底座；切换会重置为新模板默认值，再按需微调）
          </div>
          <Space wrap style={{ width: "100%", marginBottom: 8 }}>
            <Select
              style={{ minWidth: 260 }}
              value={effCode || undefined}
              placeholder="选择职位模板"
              onChange={(v) => {
                setTplCode(v);
                const t = meta.templates.find((x) => x.code === v);
                if (t) setPerms({ ...t.permissions });
              }}
              options={choices.map((t) => ({
                value: t.code,
                label: `${t.name}（${t.description || t.base_role}）`,
              }))}
            />
            <Space size={4}>
              <a style={{ fontSize: 13 }} onClick={() => setCompareOn(!compareOn)}>
                {compareOn ? "关闭模板对比" : "与模板现值对比"}
              </a>
            </Space>
          </Space>
          {cur.template_stale && !tplCode && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 8 }}
              message={`模板「${cur.template_name}」已更新到 v${cur.template_current_version}，此账号仍在 v${cur.template_version}`}
              description="编辑模板不会自动改动账号。要跟上新版本，请到「职位模板」页用「保存并同步账号」，或在这里重选模板。"
            />
          )}
          {Object.keys(cur.overrides).length > 0 && !dirty && (
            <div style={{ marginBottom: 8, fontSize: 13 }}>
              <Tag color="blue">此账号有 {Object.keys(cur.overrides).length} 处个别调整</Tag>
              <span style={{ color: "#888" }}>矩阵里以「单独开启/单独关闭」标出</span>
            </div>
          )}
          <PermissionMatrix
            meta={meta}
            value={value}
            base={base}
            compare={compareOn && tpl ? { label: "模板现值", perms: tpl.permissions } : undefined}
            onChange={(next) => setPerms(next)}
          />
        </>
      )}
    </Drawer>
  );
}
