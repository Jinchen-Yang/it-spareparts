/** 职位模板编辑抽屉：名称/说明/基础角色 + 权限矩阵。
 * 两个保存动作（产品语义 §1.2，绝不静默改账号）：
 * - 「仅保存模板」：只影响以后套用/重新套用的账号；
 * - 「保存并同步账号」：保存后弹同步预览（逐账号 diff）确认再批量刷快照。
 * 乐观锁：PUT 携带打开时的 version，409 = 他人已改，提示刷新重做。 */
import { useState } from "react";
import { Alert, Button, Drawer, Form, Input, Select, Space, message } from "antd";
import type { AccountsMeta, Perms, TemplateInfo } from "../../api/accounts";
import { createTemplate, explainApiError, updateTemplate } from "../../api/accounts";
import PermissionMatrix from "./PermissionMatrix";

const BASE_ROLES = [
  { value: "readonly", label: "只读" },
  { value: "sales", label: "销售" },
  { value: "purchaser", label: "采购" },
  { value: "boss", label: "老板" },
];

export interface TemplateDraft {
  mode: "create" | "edit" | "copy";
  source?: TemplateInfo;        // edit/copy 的来源
}

export default function TemplateDrawer({ meta, draft, onClose, onSaved, onSavedWantSync }: {
  meta: AccountsMeta;
  draft: TemplateDraft | null;
  onClose: () => void;
  onSaved: () => void;
  /** 「保存并同步账号」：保存成功后回调（带最新模板），由父层打开同步预览 */
  onSavedWantSync: (tpl: TemplateInfo) => void;
}) {
  const [form] = Form.useForm();
  const [perms, setPerms] = useState<Perms | null>(null);
  const [saving, setSaving] = useState(false);
  const src = draft?.source;
  const editing = draft?.mode === "edit";

  const value: Perms = perms ?? (src ? { ...src.permissions } : {});
  const usage = editing ? (src?.usage_count ?? 0) : 0;

  const close = () => { setPerms(null); form.resetFields(); onClose(); };

  const save = async (wantSync: boolean) => {
    if (!draft) return;
    let fields: { name: string; description?: string; base_role: string };
    try {
      fields = await form.validateFields();
    } catch { return; }
    setSaving(true);
    try {
      let saved: TemplateInfo;
      if (editing && src) {
        saved = (await updateTemplate(src.code, {
          version: src.version,
          name: fields.name,
          description: fields.description,
          ...(src.is_system ? {} : { base_role: fields.base_role }),
          permissions: value,
        })).data;
        message.success(wantSync ? "模板已保存，请确认同步范围" : "模板已保存（未同步已有账号）");
      } else {
        saved = (await createTemplate({
          name: fields.name,
          description: fields.description,
          base_role: fields.base_role,
          permissions: value,
          copy_from: draft.mode === "copy" ? src?.code : undefined,
        })).data;
        message.success("模板已创建");
      }
      onSaved();
      close();
      if (wantSync && editing) onSavedWantSync(saved);
    } catch (e) {
      const status = (e as { response?: { status?: number } })?.response?.status;
      message.error(status === 409
        ? explainApiError(e, "模板已被他人修改，请刷新后重做")
        : explainApiError(e, "保存失败"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Drawer
      title={draft?.mode === "edit" ? `编辑模板 · ${src?.name}`
        : draft?.mode === "copy" ? `复制模板 · 来自「${src?.name}」` : "新建职位模板"}
      width={720}
      open={!!draft}
      onClose={close}
      destroyOnClose
      styles={{ body: { paddingTop: 12 } }}
      extra={
        <Space>
          <Button loading={saving} onClick={() => save(false)}>仅保存模板</Button>
          {editing && (
            <Button type="primary" loading={saving} onClick={() => save(true)}
              disabled={usage === 0}>
              保存并同步账号
            </Button>
          )}
          {!editing && (
            <Button type="primary" loading={saving} onClick={() => save(false)}>创建模板</Button>
          )}
        </Space>
      }
    >
      {draft && (
        <>
          {editing && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message={`当前 ${usage} 个账号在用此模板（v${src?.version}）`}
              description="「仅保存模板」不会改动这些账号——它们保持套用时的快照；「保存并同步账号」会先展示逐账号差异预览，确认后才批量更新。"
            />
          )}
          <Form
            form={form}
            layout="vertical"
            initialValues={{
              name: draft.mode === "copy" ? `${src?.name}（副本）` : src?.name,
              description: src?.description,
              base_role: src?.base_role && src.base_role !== "admin" ? src.base_role : "readonly",
            }}
          >
            <Space wrap style={{ width: "100%" }} align="start">
              <Form.Item name="name" label="模板名称" rules={[{ required: true, message: "请输入名称" }]}
                style={{ minWidth: 220 }}>
                <Input placeholder="如：仓库管理员" />
              </Form.Item>
              <Form.Item
                name="base_role"
                label="基础角色（行级语义跟随，如销售防竞争）"
                rules={[{ required: true }]}
                style={{ minWidth: 220 }}
              >
                <Select options={BASE_ROLES} disabled={editing && src?.is_system} />
              </Form.Item>
            </Space>
            <Form.Item name="description" label="一句话说明（员工能看懂这个职位是干什么的）">
              <Input placeholder="如：管库存与型号查询，不看成本与利润" />
            </Form.Item>
          </Form>
          <PermissionMatrix meta={meta} value={value} onChange={(next) => setPerms(next)} />
        </>
      )}
    </Drawer>
  );
}
