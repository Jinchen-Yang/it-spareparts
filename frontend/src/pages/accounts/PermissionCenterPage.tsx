import PageHeader from "../../components/PageHeader";
/** 账号与权限中心（v2 壳）：账号 | 职位模板 两页签，共享一次加载的 meta。 */
import { useCallback, useEffect, useState } from "react";
import { Alert, App, Empty, Spin, Tabs } from "antd";
import type { Account, AccountsMeta } from "../../api/accounts";
import { getAccountsMeta, listAccounts } from "../../api/accounts";
import AccountsTab from "./AccountsTab";
import TemplatesTab from "./TemplatesTab";
import { useAuthz } from "../../authz/AuthzProvider";

export default function PermissionCenterPage() {
  const { message } = App.useApp();
  const authz = useAuthz();
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
      <PageHeader title="账号与权限中心" />
      {authz.snapshot?.sales_analysis.enabled && <Alert style={{ marginBottom: 16 }} type="info" showIcon
        message="销售分析已使用独立授权"
        description="销售分析的查看、导出、数据范围和可见字段，请通过账号行的「销售分析授权与客户归属」设置。旧页面权限、字段开关及职位模板仍用于其他模块，不会同步更新销售分析授权。" />}
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
