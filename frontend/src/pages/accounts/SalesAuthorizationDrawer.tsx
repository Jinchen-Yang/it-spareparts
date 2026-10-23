import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, App, Button, Checkbox, Drawer, Radio, Space, Spin, Typography } from "antd";
import {
  AUTHZ_FIELDS, getSalesAuthorization, searchAuthzCustomers, updateSalesAuthorization,
  type AuthzCustomer, type SalesAuthorization,
} from "../../api/authz";
import { explainApiError } from "../../api/accounts";
import { useAuthz } from "../../authz/AuthzProvider";
import { invalidateAuthzSession } from "../../authz/events";
import CheckboxMultiSelect from "../../components/CheckboxMultiSelect";
import { clearSessionScopedPreferences } from "../../sessionPreferences";

const customerLabel = (customer: AuthzCustomer) => `${customer.name}（ID ${customer.id}）`;
const customerOption = (customer: AuthzCustomer) => ({
  value: customer.id, label: customerLabel(customer),
  description: customer.raw_name && customer.raw_name !== customer.name ? `原始名：${customer.raw_name}` : undefined,
});

/** Each mount owns one account and one login. Parent keys by token + username. */
export default function SalesAuthorizationDrawer({ username, onClose, onSaved }: {
  username: string; onClose: () => void; onSaved: () => void;
}) {
  const authz = useAuthz();
  const { message } = App.useApp();
  const [owner] = useState(() => localStorage.getItem("token"));
  const [draft, setDraft] = useState<SalesAuthorization | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const alive = useRef(true);
  const request = useRef(0);
  const current = useCallback(() => alive.current && !!owner && localStorage.getItem("token") === owner, [owner]);
  const load = useCallback(async () => {
    if (!current() || authz.status !== "ready" || !authz.snapshot?.can_manage) return;
    const sequence = ++request.current;
    setLoading(true); setError("");
    try {
      const { data } = await getSalesAuthorization(username, owner!);
      if (!current() || sequence !== request.current) return;
      setDraft(data);
    } catch (e) {
      if (current() && sequence === request.current) setError(explainApiError(e, "授权加载失败"));
    } finally {
      if (current() && sequence === request.current) setLoading(false);
    }
  }, [current, owner, username, authz.status, authz.snapshot?.can_manage]);
  useEffect(() => {
    alive.current = true;
    void load();
    return () => { alive.current = false; ++request.current; };
  }, [load]);

  const save = async () => {
    if (!draft || saving || !current() || authz.status !== "ready" || !authz.snapshot?.can_manage) return;
    if (draft.field_permissions.some(key => key === "data_profit" || key === "data_reconciliation")
      && !draft.field_permissions.includes("data_purchase_cost")) {
      setError("查看利润、毛利率或销售对账信息需要同时勾选成本及销售金额。"); return;
    }
    setSaving(true); setError("");
    try {
      await updateSalesAuthorization(username, {
        expected_version: draft.version, actions: draft.actions, scope: draft.scope,
        field_permissions: draft.field_permissions, customer_ids: draft.customer_ids,
      }, owner!);
      if (!current()) return;
      message.success("授权和客户归属已保存，该账号原登录已失效，需要重新登录。", 6);
      const self = (authz.snapshot?.username || localStorage.getItem("username")) === username;
      if (self) {
        clearSessionScopedPreferences();
        for (const key of ["token", "username", "role", "name", "permissions", "beta_features"]) localStorage.removeItem(key);
        invalidateAuthzSession(owner);
      } else {
        onSaved();
        // Historical sessions may not carry username; /me also revokes a self-edited session.
        void authz.refresh();
      }
      onClose();
    } catch (e) {
      if (!current()) return;
      const status = (e as { response?: { status?: number } }).response?.status;
      setError(status === 409 ? "其他管理员已更新此授权。请重新加载后核对，当前草稿尚未保存。" : explainApiError(e, "授权保存失败"));
    } finally {
      if (current()) setSaving(false);
    }
  };
  if (authz.status !== "ready" || !authz.snapshot?.can_manage) return null;
  return <Drawer open width={640} title={`销售分析授权与客户归属 · ${username}`} onClose={onClose}
    extra={<Button type="primary" onClick={() => void save()} loading={saving} disabled={!draft || loading}>保存授权</Button>}>
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Alert type="info" showIcon message={authz.snapshot.sales_analysis.enabled ? "销售分析使用独立授权" : "销售分析独立授权尚未启用"}
        description={authz.snapshot.sales_analysis.enabled
          ? "此处配置销售分析权限。旧职位模板、页面权限和数据字段设置不会同步修改此处授权。"
          : "可先配置授权和客户归属；启用前，销售分析仍按原权限运行。其他模块继续使用原权限。"} />
      {error && <Alert type="error" showIcon message={error} action={<Button disabled={saving} onClick={() => void load()}>重新加载</Button>} />}
      {loading ? <Spin /> : draft && <>
        <section aria-label="销售分析操作">
          <Typography.Title level={5}>可用操作</Typography.Title>
          <Space wrap>
            <Checkbox checked={draft.actions.includes("read")} disabled={saving} onChange={e => setDraft({ ...draft, actions: e.target.checked ? ["read", ...draft.actions.filter(a => a !== "read")] : [] })}>查看销售分析</Checkbox>
            <Checkbox checked={draft.actions.includes("export")} disabled={saving || !draft.actions.includes("read")}
              onChange={e => setDraft({ ...draft, actions: e.target.checked ? [...draft.actions, "export"] : draft.actions.filter(a => a !== "export") })}>导出销售分析</Checkbox>
          </Space>
        </section>
        <section aria-label="销售分析数据范围">
          <Typography.Title level={5}>数据范围</Typography.Title>
          <Radio.Group value={draft.scope} disabled={saving} onChange={e => setDraft({ ...draft, scope: e.target.value })}>
            <Radio value="all">全部客户</Radio><Radio value="assigned_customers">仅负责客户</Radio>
          </Radio.Group>
          <p>客户归属可多人共享，覆盖该客户全部历史和后续订单。仅负责客户且未分配客户时，不可见任何订单。</p>
          <CustomerSelector owner={owner!} selected={draft.customer_ids} customers={draft.customers} disabled={saving}
            onConfirm={(ids, customers) => {
              if (current()) setDraft({ ...draft, customer_ids: ids, customers });
            }} />
          <div aria-label="已确认负责客户" style={{ marginTop: 8, overflowWrap: "anywhere" }}>
            {draft.customer_ids.map(id => <div key={id}>{customerLabel(draft.customers.find(c => c.id === id) || { id, name: "客户" })}</div>)}
          </div>
        </section>
        <section aria-label="销售分析字段权限">
          <Typography.Title level={5}>可见字段组</Typography.Title>
          <Space direction="vertical">
            {AUTHZ_FIELDS.map(field => <Checkbox key={field.id} disabled={saving} checked={draft.field_permissions.includes(field.id)}
              onChange={e => setDraft({ ...draft, field_permissions: e.target.checked ? [...draft.field_permissions, field.id] : draft.field_permissions.filter(k => k !== field.id) })}>{field.label}</Checkbox>)}
          </Space>
          <p>字段组只限制上述数据范围内的信息。利润、毛利率和销售对账信息依赖成本及销售金额。</p>
        </section>

      </>}
    </Space>
  </Drawer>;
}

function CustomerSelector({ owner, selected, customers, disabled, onConfirm }: {
  owner: string; selected: number[]; customers: AuthzCustomer[]; disabled: boolean;
  onConfirm: (ids: number[], customers: AuthzCustomer[]) => void;
}) {
  const [known, setKnown] = useState(() => new Map(customers.map(c => [c.id, c])));
  const [query, setQuery] = useState<string | null>(null);
  const [items, setItems] = useState<AuthzCustomer[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    setKnown(previous => new Map([...previous, ...customers.map(c => [c.id, c] as const)]));
  }, [customers]);
  useEffect(() => {
    if (query === null) return;
    let active = true;
    const abort = new AbortController();
    setLoading(true); setError(""); setItems([]);
    const timer = window.setTimeout(() => {
      if (localStorage.getItem("token") !== owner) return;
      void searchAuthzCustomers(query.trim(), owner, abort.signal).then(({ data }) => {
        if (!active || localStorage.getItem("token") !== owner) return;
        setItems(data.items);
        setKnown(previous => new Map([...previous, ...data.items.map(c => [c.id, c] as const)]));
      }).catch(e => {
        if (active && localStorage.getItem("token") === owner) setError(explainApiError(e, "客户搜索失败"));
      }).finally(() => { if (active && localStorage.getItem("token") === owner) setLoading(false); });
    }, 250);
    return () => { active = false; abort.abort(); window.clearTimeout(timer); };
  }, [query, owner, retry]);
  return <>
    <CheckboxMultiSelect label="负责客户" value={selected} selectionUnit="个" disabled={disabled}
      options={items.map(customerOption)}
      knownOptions={[...known.values()].map(customerOption)}
      onSearch={setQuery} loading={loading} onChange={ids => {
        if (localStorage.getItem("token") !== owner) return;
        onConfirm(ids, ids.map(id => known.get(id) || { id, name: `客户 #${id}` }));
      }} />
    {error && <Alert type="error" message={error} action={<Button onClick={() => setRetry(n => n + 1)}>重试搜索</Button>} />}
    <p>每次最多显示 50 个候选，请输入名称缩小范围。搜索不会清除已选客户；确定后仍需保存授权。</p>
  </>;
}
