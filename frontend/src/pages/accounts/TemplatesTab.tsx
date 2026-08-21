/** 职位模板管理页签：列表（使用数/版本/状态）+ 新建/复制/编辑/停用/恢复/查看账号/同步。 */
import { useState } from "react";
import { Button, Drawer, List, Popconfirm, Space, Table, Tag, message } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import type { AccountsMeta, TemplateInfo } from "../../api/accounts";
import { archiveTemplate, explainApiError, restoreTemplate, templateAccounts } from "../../api/accounts";
import TemplateDrawer, { type TemplateDraft } from "./TemplateDrawer";
import SyncModal from "./SyncModal";

const ROLE_LABEL: Record<string, string> = {
  admin: "管理员", boss: "老板", sales: "销售", purchaser: "采购", readonly: "只读", maintenance_manager: "维保负责人",
};

export default function TemplatesTab({ meta, onChanged }: {
  meta: AccountsMeta;
  onChanged: () => void;
}) {
  const [draft, setDraft] = useState<TemplateDraft | null>(null);
  const [syncTpl, setSyncTpl] = useState<TemplateInfo | null>(null);
  const [usersOf, setUsersOf] = useState<{ tpl: TemplateInfo; rows: {
    username: string; display_name: string | null; role: string; is_active: boolean;
    template_version: number | null; stale: boolean; override_count: number;
  }[] } | null>(null);

  const toggleActive = async (t: TemplateInfo) => {
    try {
      await (t.is_active ? archiveTemplate(t.code) : restoreTemplate(t.code));
      message.success(t.is_active ? "模板已停用（已套用账号不受影响）" : "模板已恢复");
      onChanged();
    } catch (e) {
      message.error(explainApiError(e, "操作失败"));
    }
  };

  const showAccounts = async (t: TemplateInfo) => {
    try {
      const r = await templateAccounts(t.code);
      setUsersOf({ tpl: t, rows: r.data.accounts });
    } catch (e) {
      message.error(explainApiError(e, "加载失败"));
    }
  };

  const enabledCount = (t: TemplateInfo) =>
    Object.values(t.permissions).filter(Boolean).length;

  const columns = [
    { title: "模板", dataIndex: "name",
      render: (v: string, t: TemplateInfo) => (
        <Space direction="vertical" size={0}>
          <Space size={6}>
            <b>{v}</b>
            {t.is_system && <Tag>内置</Tag>}
            {t.locked && <Tag color="red">锁定</Tag>}
            {!t.is_active && <Tag color="default">已停用</Tag>}
            {t.permission_combo_errors.length > 0 && <Tag color="red">权限组合需修复</Tag>}
          </Space>
          <span style={{ fontSize: 12, color: "#888" }}>{t.description || t.code}</span>
        </Space>
      ) },
    { title: "基础角色", dataIndex: "base_role",
      render: (v: string) => <Tag>{ROLE_LABEL[v] || v}</Tag> },
    { title: "开启权限", key: "cnt",
      render: (_: unknown, t: TemplateInfo) => `${enabledCount(t)} / ${meta.all_keys.length} 项` },
    { title: "使用账号", dataIndex: "usage_count",
      render: (v: number, t: TemplateInfo) => v > 0
        ? <a onClick={() => showAccounts(t)}>{v} 个</a>
        : <span style={{ color: "#999" }}>0 个</span> },
    { title: "版本", dataIndex: "version", render: (v: number) => `v${v}` },
    { title: "操作", key: "op",
      render: (_: unknown, t: TemplateInfo) => t.locked
        ? <span style={{ color: "#999" }}>系统锁定</span>
        : (
          <Space size="small" wrap>
            <a onClick={() => setDraft({ mode: "edit", source: t })}>编辑</a>
            <a onClick={() => setDraft({ mode: "copy", source: t })}>复制</a>
            {t.usage_count > 0 && t.is_active && (
              <a onClick={() => setSyncTpl(t)}>同步账号</a>
            )}
            {!t.is_system && (
              <Popconfirm
                title={t.is_active ? "停用该模板？之后不能再套用，已套用账号不受影响。" : "恢复该模板？"}
                onConfirm={() => toggleActive(t)}
              >
                <a style={{ color: t.is_active ? "#cf1322" : "#389e0d" }}>
                  {t.is_active ? "停用" : "恢复"}
                </a>
              </Popconfirm>
            )}
          </Space>
        ) },
  ];

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, flexWrap: "wrap", gap: 8 }}>
        <span style={{ fontSize: 13, color: "#888" }}>
          职位模板是可编辑的权限预设：编辑后已套用账号<b>不会自动变</b>，需用「同步账号」显式推送。
        </span>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setDraft({ mode: "create" })}>
          新建模板
        </Button>
      </div>
      <Table
        rowKey="code"
        size="middle"
        columns={columns as never}
        dataSource={meta.templates}
        pagination={false}
        scroll={{ x: 720 }}
      />

      <TemplateDrawer
        meta={meta}
        draft={draft}
        onClose={() => setDraft(null)}
        onSaved={onChanged}
        onSavedWantSync={(tpl) => setSyncTpl(tpl)}
      />
      <SyncModal
        template={syncTpl}
        onClose={() => setSyncTpl(null)}
        onDone={onChanged}
      />

      <Drawer
        title={usersOf ? `使用「${usersOf.tpl.name}」的账号（${usersOf.rows.length}）` : ""}
        open={!!usersOf}
        width={420}
        onClose={() => setUsersOf(null)}
      >
        <List
          size="small"
          dataSource={usersOf?.rows || []}
          renderItem={(u) => (
            <List.Item>
              <Space size={6} wrap>
                <b>{u.display_name || u.username}</b>
                <span style={{ color: "#999" }}>({u.username})</span>
                {!u.is_active && <Tag>停用</Tag>}
                {u.stale && <Tag color="orange">模板落后 v{u.template_version}</Tag>}
                {u.override_count > 0 && <Tag color="blue">{u.override_count} 处调整</Tag>}
              </Space>
            </List.Item>
          )}
        />
      </Drawer>
    </>
  );
}
