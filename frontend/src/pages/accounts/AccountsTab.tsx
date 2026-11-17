import { formatBusinessDateTime } from "../../utils/date";
/** 账号列表页签：筛选（模板/角色/状态/关键词）+ 多选批量 + 单账号操作。 */
import { useMemo, useState } from "react";
import { App, Alert, Button, Checkbox, Descriptions, Drawer, Empty, Form, Input, List, Modal, Popconfirm, Select, Space, Table, Tag, Typography } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import ResizableTable from "../../components/ResizableTable";
import type { Account, AccountsMeta, BulkOperation, DeletedAccount, DocumentsPreview } from "../../api/accounts";
import {
  createAccount, deleteAccount, documentsPreview, explainApiError, getActivity,
  listDeletedAccounts, resetPassword, restoreAccount, setAccountActive, setDocumentsVisibility,
} from "../../api/accounts";
import AccountDrawer from "./AccountDrawer";
import BulkModal from "./BulkModal";

const ROLE_LABEL: Record<string, string> = {
  admin: "管理员", boss: "老板", sales: "销售", purchaser: "采购", readonly: "只读", maintenance_manager: "维保负责人",
};
const fmt = formatBusinessDateTime;

export default function AccountsTab({ meta, accounts, loading, onChanged }: {
  meta: AccountsMeta;
  accounts: Account[];
  loading: boolean;
  onChanged: () => void;
}) {
  const { message } = App.useApp();
  const [fTpl, setFTpl] = useState<string>();
  const [fRole, setFRole] = useState<string>();
  const [fActive, setFActive] = useState<string>();
  const [kw, setKw] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [bulkOp, setBulkOp] = useState<BulkOperation | null>(null);
  const [permT, setPermT] = useState<Account | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [pwT, setPwT] = useState<Account | null>(null);
  const [actT, setActT] = useState<Account | null>(null);
  const [act, setAct] = useState<{
    last_login_at?: string | null; total_actions?: number;
    recent?: { action: string; resource: string | null; at: string | null }[];
    changes?: { action: string; by: string | null; at: string | null; reason?: string | null }[];
  } | null>(null);
  const [cForm] = Form.useForm();
  const [pwForm] = Form.useForm();
  const [delT, setDelT] = useState<Account | null>(null);
  const [delPreview, setDelPreview] = useState<DocumentsPreview | null>(null);
  const [delPreviewError, setDelPreviewError] = useState(false);
  const [delHide, setDelHide] = useState(false);
  const [delBusy, setDelBusy] = useState(false);
  const [deletedOpen, setDeletedOpen] = useState(false);
  const [deletedRows, setDeletedRows] = useState<DeletedAccount[] | null>(null);
  // 删除账号仅管理员可见（后端仍强校验，前端只是隐藏入口）
  const isAdmin = localStorage.getItem("role") === "admin";

  const rows = useMemo(() => accounts.filter((u) => {
    if (fTpl && u.template_code !== fTpl) return false;
    if (fRole && u.role !== fRole) return false;
    if (fActive === "on" && !u.is_active) return false;
    if (fActive === "off" && u.is_active) return false;
    if (kw.trim()) {
      const hay = `${u.username} ${u.display_name || ""} ${u.salesperson_name || ""}`.toLowerCase();
      if (!hay.includes(kw.trim().toLowerCase())) return false;
    }
    return true;
  }), [accounts, fTpl, fRole, fActive, kw]);

  const selectedAccounts = accounts.filter((u) => selected.includes(u.username));

  const doCreate = async (v: {
    username: string; password: string; display_name?: string;
    salesperson_name?: string; template_code: string;
  }) => {
    try {
      await createAccount(v);
      message.success("已建号");
      setCreateOpen(false);
      cForm.resetFields();
      onChanged();
    } catch (e) {
      message.error(explainApiError(e, "建号失败"));
    }
  };

  const doReset = async (v: { password: string }) => {
    if (!pwT) return;
    try {
      await resetPassword(pwT.username, v.password);
      message.success("密码已重置，该用户旧登录已失效");
      setPwT(null);
      pwForm.resetFields();
    } catch (e) {
      message.error(explainApiError(e, "重置失败"));
    }
  };

  const toggleActive = async (u: Account) => {
    try {
      await setAccountActive(u.username, !u.is_active);
      onChanged();
    } catch (e) {
      message.error(explainApiError(e, "操作失败"));
    }
  };

  const removeAccount = async (u: Account) => {
    // D-0063：删除弹窗带单据量预览与「同时隐藏其名下销售单」勾选；可恢复。
    setDelT(u);
    setDelHide(false);
    setDelPreview(null);
    setDelPreviewError(false);
    try {
      const r = await documentsPreview(u.username);
      setDelPreview(r.data);
    } catch {
      // 预览失败不阻塞删除，按无隐藏处理（D-0063 口径：无映射不隐藏）
      setDelPreviewError(true);
    }
  };

  const confirmDelete = async () => {
    if (!delT) return;
    setDelBusy(true);
    try {
      const hide = delHide && !!delPreview?.can_hide;
      await deleteAccount(delT.username, hide);
      message.success(hide
        ? `已删除账号 ${delT.username}，其名下销售单已在销售/采购分析隐藏（可恢复）`
        : `已删除账号 ${delT.username}`);
      setDelT(null);
      if (deletedOpen) await refreshDeleted();   // 面板展开时保持数据新鲜
      onChanged();
    } catch (e) {
      message.error(explainApiError(e, "删除失败"));
    } finally {
      setDelBusy(false);
    }
  };

  const refreshDeleted = async () => {
    try {
      const r = await listDeletedAccounts();
      setDeletedRows(r.data);
    } catch (e) {
      message.error(explainApiError(e, "已删除账号加载失败"));
    }
  };

  const loadDeleted = (open: boolean) => {
    setDeletedOpen(open);
    if (open && deletedRows === null) void refreshDeleted();
  };

  const doRestore = async (u: DeletedAccount) => {
    try {
      await restoreAccount(u.username);
      message.success(`已恢复账号 ${u.username}（单据显示一并恢复；启用请到列表操作）`);
      await refreshDeleted();
      onChanged();
    } catch (e) {
      message.error(explainApiError(e, "恢复失败"));
    }
  };

  const toggleHidden = async (u: DeletedAccount) => {
    try {
      await setDocumentsVisibility(u.username, !u.hide_documents);
      await refreshDeleted();
    } catch (e) {
      message.error(explainApiError(e, "切换失败"));
    }
  };

  const openActivity = async (u: Account) => {
    setActT(u); setAct(null);
    try {
      const r = await getActivity(u.username);
      setAct(r.data);
    } catch {
      setAct({ recent: [] });
    }
  };

  const columns = [
    { title: "用户名", dataIndex: "username", render: (v: string) => <b>{v}</b> },
    { title: "姓名", dataIndex: "display_name", render: (v: string) => v || "—" },
    { title: "职位模板", key: "tpl", render: (_: unknown, u: Account) => (
      <Space size={4} wrap>
        <Tag color="geekblue">{u.template_name || u.template_code || "—"}</Tag>
        {u.template_stale && <Tag color="orange">模板已更新</Tag>}
        {u.is_custom && <Tag color="blue">{Object.keys(u.overrides).length} 处调整</Tag>}
        {u.permission_combo_errors.length > 0 && <Tag color="red">权限组合需修复</Tag>}
      </Space>
    ) },
    { title: "角色", dataIndex: "role", render: (v: string) => <Tag>{ROLE_LABEL[v] || v}</Tag> },
    { title: "状态", dataIndex: "is_active",
      render: (v: boolean) => v ? <Tag color="green">启用</Tag> : <Tag>停用</Tag> },
    { title: "最近登录", dataIndex: "last_login_at", render: fmt },
    { title: "操作", key: "op", render: (_: unknown, u: Account) => u.username === "admin"
      ? <span style={{ color: "#999" }}>系统账号</span>
      : (
        <Space size="small">
          <Button type="link" size="small" onClick={() => setPermT(u)}>权限</Button>
          <Button type="link" size="small" onClick={() => setPwT(u)}>改密</Button>
          <Button type="link" size="small" onClick={() => openActivity(u)}>活动</Button>
          <Popconfirm title={u.is_active ? "停用该账号？" : "启用该账号？"} onConfirm={() => toggleActive(u)}>
            <Button type="link" size="small" danger={u.is_active}>{u.is_active ? "停用" : "启用"}</Button>
          </Popconfirm>
          {isAdmin && u.username !== "admin" && u.role !== "admin" && (
            <Button type="link" size="small" danger onClick={() => removeAccount(u)}>删除</Button>
          )}
        </Space>
      ) },
  ];

  return (
    <>
      <Space wrap style={{ marginBottom: 12, width: "100%", justifyContent: "space-between" }}>
        <Space wrap>
          <Select
            allowClear placeholder="职位模板" style={{ minWidth: 140 }}
            value={fTpl} onChange={setFTpl}
            options={meta.templates.map((t) => ({ value: t.code, label: t.name }))}
          />
          <Select
            allowClear placeholder="角色" style={{ minWidth: 110 }}
            value={fRole} onChange={setFRole}
            options={meta.roles.map((r) => ({ value: r, label: ROLE_LABEL[r] || r }))}
          />
          <Select
            allowClear placeholder="状态" style={{ minWidth: 100 }}
            value={fActive} onChange={setFActive}
            options={[{ value: "on", label: "启用" }, { value: "off", label: "停用" }]}
          />
          <Input.Search
            allowClear placeholder="用户名 / 姓名" style={{ width: 180 }}
            value={kw} onChange={(e) => setKw(e.target.value)}
          />
        </Space>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
          新建账号
        </Button>
      </Space>

      {selected.length > 0 && (
        <Alert
          type="info"
          style={{ marginBottom: 12 }}
          message={
            <Space wrap>
              <span>已选 {selected.length} 个账号：</span>
              <Button size="small" onClick={() => setBulkOp("apply_template")}>批量套用模板</Button>
              <Button size="small" onClick={() => setBulkOp("grant")}>批量增加权限</Button>
              <Button size="small" onClick={() => setBulkOp("revoke")}>批量取消权限</Button>
              <Button size="small" onClick={() => setBulkOp("reset_to_template")}>恢复模板默认</Button>
              <Button type="link" size="small" onClick={() => setSelected([])}>清除选择</Button>
            </Space>
          }
        />
      )}

      <ResizableTable
        storageKey="accounts-v2"
        rowKey="username"
        dataSource={rows}
        columns={columns as never}
        loading={loading}
        pagination={false}
        size="middle"
        scroll={{ x: 760 }}
        rowSelection={{
          selectedRowKeys: selected,
          onChange: (ks: React.Key[]) => setSelected(ks as string[]),
          getCheckboxProps: (u: Account) => ({
            // 内置 admin 与管理员账号不参与批量（后端同样整体拒绝）
            disabled: u.username === "admin" || u.role === "admin",
          }),
        }}
      />

      {isAdmin && (
        <div style={{ marginTop: 16 }}>
          <Button size="small" type={deletedOpen ? "primary" : "default"}
            onClick={() => loadDeleted(!deletedOpen)}>
            {deletedOpen ? "收起已删除账号" : "已删除账号"}
          </Button>
          {deletedOpen && (
            <Table<DeletedAccount>
              size="small" rowKey="username" style={{ marginTop: 8 }}
              loading={deletedRows === null}
              dataSource={deletedRows || []}
              pagination={false}
              locale={{ emptyText: "没有已删除账号" }}
              columns={[
                { title: "用户名", dataIndex: "username", render: (v: string) => <b>{v}</b> },
                { title: "姓名", dataIndex: "display_name", render: (v: string | null) => v || "—" },
                { title: "对应销售名", dataIndex: "salesperson_name",
                  render: (v: string | null) => v || "—" },
                { title: "删除时间", dataIndex: "deleted_at", render: fmt },
                { title: "名下单据", dataIndex: "hide_documents",
                  render: (v: boolean, u: DeletedAccount) => v
                    ? <Tag color="orange">已隐藏（{u.salesperson_name || "—"}）</Tag>
                    : <Tag>显示中</Tag> },
                { title: "操作", key: "op", render: (_: unknown, u: DeletedAccount) => (
                  <Space size="small">
                    <Button type="link" size="small" onClick={() => doRestore(u)}>恢复账号</Button>
                    {u.salesperson_name && (
                      <Button type="link" size="small"
                        onClick={() => toggleHidden(u)}>
                        {u.hide_documents ? "恢复单据显示" : "隐藏其单据"}
                      </Button>
                    )}
                  </Space>
                ) },
              ]}
            />
          )}
        </div>
      )}

      <Modal
        title={delT ? `删除账号 ${delT.username}` : ""}
        open={!!delT}
        confirmLoading={delBusy}
        okText="删除" okButtonProps={{ danger: true }}
        onCancel={() => setDelT(null)}
        onOk={confirmDelete}
      >
        <Descriptions column={1} size="small" style={{ marginBottom: 12 }}>
          <Descriptions.Item label="姓名">{delT?.display_name || "—"}</Descriptions.Item>
          <Descriptions.Item label="对应销售名">
            {delPreview ? (delPreview.salesperson_name || "未配置") : "…"}
          </Descriptions.Item>
          {delPreview?.can_hide && (
            <Descriptions.Item label="名下单据（已生效）">
              销售单 {delPreview.sales_order_count} 张、关联采购单 {delPreview.linked_purchase_order_count} 张
            </Descriptions.Item>
          )}
        </Descriptions>
        <Typography.Text type="secondary" style={{ display: "block", marginBottom: 8 }}>
          删除后该账号无法登录，历史记录保留；可在「已删除账号」里恢复。
        </Typography.Text>
        {delPreview ? (delPreview.can_hide ? (
          <Checkbox checked={delHide} onChange={(e) => setDelHide(e.target.checked)}>
            同时隐藏其名下销售单（销售/采购分析不再显示，汇总金额不变）
          </Checkbox>
        ) : (
          <Typography.Text type="secondary">
            该账号未配置「对应销售名」，无法定位其名下单据；如需隐藏请先在编辑中补配。
          </Typography.Text>
        )) : delPreviewError ? (
          <Typography.Text type="secondary">单据预览加载失败；删除仍可进行，隐藏不可选。</Typography.Text>
        ) : <Typography.Text type="secondary">正在加载单据预览…</Typography.Text>}
      </Modal>

      <BulkModal
        meta={meta}
        operation={bulkOp}
        targets={selectedAccounts}
        onClose={() => setBulkOp(null)}
        onDone={() => { setSelected([]); onChanged(); }}
      />

      <AccountDrawer
        meta={meta}
        account={permT}
        onClose={() => setPermT(null)}
        onSaved={onChanged}
      />

      <Modal title="新建账号" open={createOpen} onOk={() => cForm.submit()}
        onCancel={() => setCreateOpen(false)} okText="创建">
        <Form form={cForm} layout="vertical" onFinish={doCreate}
          initialValues={{ template_code: "readonly" }}>
          <Form.Item name="username" label="用户名（登录用）"
            rules={[{ required: true, message: "请输入用户名" }]}>
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item name="display_name" label="姓名"><Input /></Form.Item>
          <Form.Item name="template_code" label="职位模板（决定默认权限，建号后可微调）"
            rules={[{ required: true }]}>
            <Select options={meta.templates.filter((t) => t.is_active && !t.locked).map((t) => ({
              value: t.code, label: `${t.name}（${t.description || t.base_role}）`,
            }))} />
          </Form.Item>
          <Form.Item name="salesperson_name" label="对应销售名（销售防竞争用，选填）">
            <Input placeholder="与销售数据里的销售员姓名一致" />
          </Form.Item>
          <Form.Item name="password" label="初始密码（≥6 位）"
            rules={[{ required: true, min: 6, message: "至少 6 位" }]}>
            <Input.Password autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal title={pwT ? `重置密码 · ${pwT.username}` : ""} open={!!pwT}
        onOk={() => pwForm.submit()} onCancel={() => setPwT(null)} okText="重置">
        <Form form={pwForm} layout="vertical" onFinish={doReset}>
          <Form.Item name="password" label="新密码（≥6 位）"
            rules={[{ required: true, min: 6, message: "至少 6 位" }]}>
            <Input.Password autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer title={actT ? `活动 · ${actT.display_name || actT.username}` : ""} width={440}
        open={!!actT} onClose={() => setActT(null)}>
        {act ? (
          <>
            <Descriptions column={1} size="small" style={{ marginBottom: 12 }}>
              <Descriptions.Item label="最近登录">{fmt(act.last_login_at ?? null)}</Descriptions.Item>
              <Descriptions.Item label="累计操作">{act.total_actions ?? 0} 次</Descriptions.Item>
            </Descriptions>
            {act.changes?.length ? (
              <List size="small" header="账号变更记录（谁改的）" style={{ marginBottom: 12 }}
                dataSource={act.changes.slice(0, 10)}
                renderItem={(it) => (
                  <List.Item>
                    <Space size={6} wrap>
                      <Tag>{it.action}</Tag>
                      <span>{it.by || "—"}</span>
                      <span style={{ color: "#999", fontSize: 12 }}>{fmt(it.at)}</span>
                      {it.reason && <span style={{ color: "#999", fontSize: 12 }}>{it.reason}</span>}
                    </Space>
                  </List.Item>
                )} />
            ) : null}
            {act.recent?.length ? (
              <List size="small" header="最近查询" dataSource={act.recent}
                renderItem={(it) => (
                  <List.Item>
                    <Space><Tag>{it.action}</Tag><span>{it.resource || "—"}</span>
                      <span style={{ color: "#999", fontSize: 12 }}>{fmt(it.at)}</span></Space>
                  </List.Item>
                )} />
            ) : <Empty description="暂无活动记录" />}
          </>
        ) : <Empty description="加载中…" />}
      </Drawer>
    </>
  );
}
