import { useEffect, useState } from "react";
import {
  Button, Modal, Drawer, Form, Input, Select, Checkbox, Tag,
  Space, message, Popconfirm, Divider, Descriptions, List, Empty,
} from "antd";
import ResizableTable from "../components/ResizableTable";
import api from "../api";

type Perms = Record<string, boolean>;
type Account = {
  username: string; display_name: string | null; role: string;
  salesperson_name: string | null; is_active: boolean;
  last_login_at: string | null; permissions: Perms; is_custom: boolean;
};
type Meta = {
  roles: string[]; data_keys: string[]; page_keys: string[]; row_keys: string[];
  labels: Record<string, string>; role_templates: Record<string, Perms>;
};

const ROLE_LABEL: Record<string, string> = {
  admin: "管理员", boss: "老板", sales: "销售", purchaser: "采购", readonly: "只读",
};
const fmt = (s: string | null) => (s ? new Date(s).toLocaleString("zh-CN") : "—");

export default function AccountsPage() {
  const [rows, setRows] = useState<Account[]>([]);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [permT, setPermT] = useState<Account | null>(null);
  const [editPerms, setEditPerms] = useState<Perms>({});
  const [editRole, setEditRole] = useState<string>("sales");
  const [pwT, setPwT] = useState<Account | null>(null);
  const [actT, setActT] = useState<Account | null>(null);
  const [act, setAct] = useState<any>(null);
  const [cForm] = Form.useForm();
  const [pwForm] = Form.useForm();

  const load = async () => {
    setLoading(true);
    try {
      const [a, m] = await Promise.all([api.get("/accounts"), api.get("/accounts/_meta")]);
      setRows(a.data); setMeta(m.data);
    } catch { message.error("加载账号失败"); } finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  const openPerm = (u: Account) => { setPermT(u); setEditRole(u.role); setEditPerms({ ...u.permissions }); };
  const applyTemplate = (role: string) => {
    setEditRole(role);
    if (meta) setEditPerms({ ...meta.role_templates[role] });
  };
  const savePerm = async () => {
    if (!permT) return;
    try {
      await api.put(`/accounts/${permT.username}`, { role: editRole, permissions: editPerms });
      message.success("权限已保存（该用户下次登录生效）"); setPermT(null); load();
    } catch (e: any) { message.error(e?.response?.data?.detail || "保存失败"); }
  };

  const doCreate = async (v: any) => {
    try { await api.post("/accounts", v); message.success("已建号"); setCreateOpen(false); cForm.resetFields(); load(); }
    catch (e: any) { message.error(e?.response?.data?.detail || "建号失败"); }
  };
  const doReset = async (v: any) => {
    if (!pwT) return;
    try { await api.put(`/accounts/${pwT.username}/password`, v); message.success("密码已重置"); setPwT(null); pwForm.resetFields(); }
    catch (e: any) { message.error(e?.response?.data?.detail || "重置失败"); }
  };
  const toggleActive = async (u: Account) => {
    try { await api.put(`/accounts/${u.username}/active`, { is_active: !u.is_active }); load(); }
    catch (e: any) { message.error(e?.response?.data?.detail || "操作失败"); }
  };
  const openActivity = async (u: Account) => {
    setActT(u); setAct(null);
    try { const r = await api.get(`/accounts/${u.username}/activity`); setAct(r.data); } catch { setAct({ recent: [] }); }
  };

  const columns = [
    { title: "用户名", dataIndex: "username", render: (v: string, r: Account) => <b>{v}</b> },
    { title: "姓名", dataIndex: "display_name", render: (v: string) => v || "—" },
    { title: "角色", dataIndex: "role", render: (v: string) => <Tag>{ROLE_LABEL[v] || v}</Tag> },
    { title: "状态", dataIndex: "is_active", render: (v: boolean) => v ? <Tag color="green">启用</Tag> : <Tag>停用</Tag> },
    { title: "最近登录", dataIndex: "last_login_at", render: fmt },
    {
      title: "操作", key: "op", render: (_: any, u: Account) => u.username === "admin"
        ? <span style={{ color: "#999" }}>系统账号</span>
        : <Space size="small">
            <a onClick={() => openPerm(u)}>权限</a>
            <a onClick={() => setPwT(u)}>改密</a>
            <a onClick={() => openActivity(u)}>活动</a>
            <Popconfirm title={u.is_active ? "停用该账号？" : "启用该账号？"} onConfirm={() => toggleActive(u)}>
              <a style={{ color: u.is_active ? "#cf1322" : "#389e0d" }}>{u.is_active ? "停用" : "启用"}</a>
            </Popconfirm>
          </Space>,
    },
  ];

  const permGroup = (title: string, keys: string[]) => meta && (
    <>
      <Divider orientation="left" plain style={{ margin: "12px 0 8px", fontSize: 13 }}>{title}</Divider>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px 16px" }}>
        {keys.map((k) => (
          <Checkbox key={k} checked={!!editPerms[k]}
            onChange={(e) => setEditPerms({ ...editPerms, [k]: e.target.checked })}>
            {meta.labels[k] || k}
          </Checkbox>
        ))}
      </div>
    </>
  );

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h2 style={{ margin: 0 }}>账号管理</h2>
        <Button type="primary" onClick={() => setCreateOpen(true)}>新建账号</Button>
      </div>
      <ResizableTable storageKey="accounts" rowKey="username" dataSource={rows} columns={columns as any} loading={loading} pagination={false} size="middle" />

      <Modal title="新建账号" open={createOpen} onOk={() => cForm.submit()} onCancel={() => setCreateOpen(false)} okText="创建">
        <Form form={cForm} layout="vertical" onFinish={doCreate} initialValues={{ role: "sales" }}>
          <Form.Item name="username" label="用户名（登录用）" rules={[{ required: true, message: "请输入用户名" }]}><Input /></Form.Item>
          <Form.Item name="display_name" label="姓名"><Input /></Form.Item>
          <Form.Item name="role" label="角色（决定默认权限，可建后微调）" rules={[{ required: true }]}>
            <Select options={(meta?.roles || []).filter((r) => r !== "admin").map((r) => ({ value: r, label: ROLE_LABEL[r] || r }))} />
          </Form.Item>
          <Form.Item name="salesperson_name" label="对应销售名（销售防竞争用，选填）"><Input placeholder="与销售数据里的销售员姓名一致" /></Form.Item>
          <Form.Item name="password" label="初始密码（≥6 位）" rules={[{ required: true, min: 6, message: "至少 6 位" }]}><Input.Password /></Form.Item>
        </Form>
      </Modal>

      <Drawer title={permT ? `权限设置 · ${permT.display_name || permT.username}` : ""} width={460}
        open={!!permT} onClose={() => setPermT(null)}
        extra={<Button type="primary" onClick={savePerm}>保存权限</Button>}>
        {permT && meta && (
          <>
            <div style={{ marginBottom: 8, fontSize: 13, color: "#888" }}>套用角色模板（会覆盖下面勾选）</div>
            <Select value={editRole} style={{ width: "100%", marginBottom: 4 }} onChange={applyTemplate}
              options={meta.roles.filter((r) => r !== "admin").map((r) => ({ value: r, label: ROLE_LABEL[r] || r }))} />
            {permGroup("能看哪些数据", meta.data_keys)}
            {permGroup("能用哪些页面", meta.page_keys)}
            {permGroup("防恶性竞争", meta.row_keys)}
          </>
        )}
      </Drawer>

      <Modal title={pwT ? `重置密码 · ${pwT.username}` : ""} open={!!pwT} onOk={() => pwForm.submit()} onCancel={() => setPwT(null)} okText="重置">
        <Form form={pwForm} layout="vertical" onFinish={doReset}>
          <Form.Item name="password" label="新密码（≥6 位）" rules={[{ required: true, min: 6, message: "至少 6 位" }]}><Input.Password /></Form.Item>
        </Form>
      </Modal>

      <Drawer title={actT ? `活动 · ${actT.display_name || actT.username}` : ""} width={420} open={!!actT} onClose={() => setActT(null)}>
        {act ? (
          <>
            <Descriptions column={1} size="small" style={{ marginBottom: 12 }}>
              <Descriptions.Item label="最近登录">{fmt(act.last_login_at)}</Descriptions.Item>
              <Descriptions.Item label="累计操作">{act.total_actions ?? 0} 次</Descriptions.Item>
            </Descriptions>
            {act.recent?.length ? (
              <List size="small" header="最近查询" dataSource={act.recent}
                renderItem={(it: any) => (
                  <List.Item><Space><Tag>{it.action}</Tag><span>{it.resource || "—"}</span>
                    <span style={{ color: "#999", fontSize: 12 }}>{fmt(it.at)}</span></Space></List.Item>
                )} />
            ) : <Empty description="暂无活动记录" />}
          </>
        ) : <Empty description="加载中…" />}
      </Drawer>
    </>
  );
}
