import { accountAccess } from "../../authz/accountAccess";
import UnifiedPermissionCenter from "./UnifiedPermissionCenter";
import PageHeader from "../../components/PageHeader";
/** 账号与权限中心（v2 壳）：账号 | 职位模板 两页签，共享一次加载的 meta。 */
import { useCallback, useEffect, useState } from "react";
import { Alert, App, Empty, Spin, Tabs } from "antd";
import type { Account, AccountsMeta } from "../../api/accounts";
import { getAccountsMeta, listAccounts } from "../../api/accounts";
import AccountsTab from "./AccountsTab";
import TemplatesTab from "./TemplatesTab";
import { actorAllowed, AuthzUnavailable, useAuthz } from "../../authz/AuthzProvider";

function LegacyPermissionCenterPage() {
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
        description="销售分析旧改权入口已停用；请在统一模块授权模式下使用「权限与范围」。旧页面权限、字段开关及职位模板仍用于其他模块，不会同步更新销售分析授权。" />}
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

export default function PermissionCenterPage() {
 const authz=useAuthz();
 if(authz.status!=="ready"||!authz.snapshot)return <AuthzUnavailable/>;
 if(!accountAccess(authz.snapshot).enter)return <Alert type="warning" message="没有账号中心访问权限"/>;
 return authz.snapshot.authorization_mode==="unified_modules"?<UnifiedPermissionCenter key={authz.snapshot.version}/>:<LegacyPermissionCenterPage/>;
}
