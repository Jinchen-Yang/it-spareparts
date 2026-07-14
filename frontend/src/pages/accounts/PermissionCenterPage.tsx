/** 账号与权限中心（v2 壳）：账号 | 职位模板 两页签，共享一次加载的 meta。 */
import { useCallback, useEffect, useState } from "react";
import { Empty, Spin, Tabs, message } from "antd";
import type { Account, AccountsMeta } from "../../api/accounts";
import { getAccountsMeta, listAccounts } from "../../api/accounts";
import AccountsTab from "./AccountsTab";
import TemplatesTab from "./TemplatesTab";

export default function PermissionCenterPage() {
  const [meta, setMeta] = useState<AccountsMeta | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [a, m] = await Promise.all([listAccounts(), getAccountsMeta()]);
      setAccounts(a.data);
      setMeta(m.data);
    } catch {
      message.error("加载账号与权限数据失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>账号与权限中心</h2>
      </div>
      {!meta ? (
        loading ? <Spin style={{ display: "block", margin: "80px auto" }} />
          : <Empty description="加载失败，请刷新重试" />
      ) : (
        <Tabs
          defaultActiveKey="accounts"
          items={[
            {
              key: "accounts",
              label: `账号（${accounts.length}）`,
              children: (
                <AccountsTab meta={meta} accounts={accounts} loading={loading} onChanged={load} />
              ),
            },
            {
              key: "templates",
              label: `职位模板（${meta.templates.length}）`,
              children: <TemplatesTab meta={meta} onChanged={load} />,
            },
          ]}
        />
      )}
    </>
  );
}
