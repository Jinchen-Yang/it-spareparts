import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Input,
  Popconfirm,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
} from "antd";
import PageHeader from "../../components/PageHeader";
import { actorAllowed, useAuthz } from "../../authz/AuthzProvider";
import {
  authzCommand,
  getAuthzCatalog,
  safeVersion,
  type AuthzCatalog,
  type Target,
} from "../../api/authz";
import {
  listCanonicalAccounts,
  listCanonicalTemplates,
  type CanonicalAccount,
  type CanonicalTemplate,
} from "../../api/accounts";
import { useAuthorizationTask } from "./useAuthorizationTask";
import ModuleAccountDrawer from "./ModuleAccountDrawer";
import ModuleTemplateDrawer from "./ModuleTemplateDrawer";
import ModuleBulkModal from "./ModuleBulkModal";
import AccountCommandModal, {
  type AccountCommandKind,
} from "./AccountCommandModal";
export default function UnifiedPermissionCenter() {
  const { snapshot } = useAuthz();
  const task = useAuthorizationTask("permission-center");
  const [catalog, setCatalog] = useState<AuthzCatalog | null>(null),
    [accounts, setAccounts] = useState<CanonicalAccount[]>([]),
    [templates, setTemplates] = useState<CanonicalTemplate[]>([]),
    [loaded, setLoaded] = useState(false),
    [selected, setSelected] = useState<number[]>([]),
    [query, setQuery] = useState(""),
    [accountTarget, setAccountTarget] = useState<string | null>(null),
    [templateTarget, setTemplateTarget] = useState<
      CanonicalTemplate | null | undefined
    >(),
    [command, setCommand] = useState<{
      kind: AccountCommandKind;
      account?: CanonicalAccount;
    } | null>(null),
    [bulk, setBulk] = useState<{
      targets: Target[];
      sync?: CanonicalTemplate;
    } | null>(null);
  const can = (cap: Parameters<typeof actorAllowed>[1]) =>
    actorAllowed(snapshot, cap);
  const needCatalog =
    can("manage_authorization") ||
    can("manage_templates") ||
    can("create_accounts");
  const load = () =>
    task.run(
      async () => {
        const [a, c, t] = await Promise.all([
          listCanonicalAccounts(task.owner),
          needCatalog ? getAuthzCatalog(task.owner) : Promise.resolve(null),
          can("manage_templates")
            ? listCanonicalTemplates(task.owner).then((r) => r.data)
            : Promise.resolve([]),
        ]);
        for (const row of a.data)
          if (
            !safeVersion(row.id) ||
            !row.id ||
            !safeVersion(row.authorization_epoch) ||
            !row.authorization_summary ||
            !Array.isArray(row.authorization_summary.override_resources)
          )
            throw new Error("账号摘要协议无效");
        for (const row of t)
          if (
            !safeVersion(row.version) ||
            !row.authorization_summary ||
            !Array.isArray(row.authorization_summary.configured_resources)
          )
            throw new Error("模板摘要协议无效");
        return { accounts: a.data, catalog: c, templates: t };
      },
      (v) => {
        setAccounts(v.accounts);
        setCatalog(v.catalog);
        setTemplates(v.templates);
        setLoaded(true);
        setSelected([]);
      },
    );
  useEffect(() => {
    void load();
  }, []);
  const envelope = {
    schema_version: 1,
    catalog_version: snapshot!.catalog_version,
  };
  const fallbackCatalog: AuthzCatalog = {
    ...envelope,
    schema_version: 1,
    authorization_mode: "unified_modules",
    modules: [],
  };
  const targets = (rows: CanonicalAccount[]): Target[] =>
    rows.map((u) => ({
      user_id: u.id,
      username: u.username,
      expected_epoch: u.authorization_epoch,
    }));
  const lifecycle = (u: CanonicalAccount, operation: "active" | "delete") =>
    task.run(
      () =>
        authzCommand(
          `/accounts/${encodeURIComponent(u.username)}${operation === "active" ? "/active" : ""}`,
          operation === "active" ? "put" : "delete",
          {
            ...envelope,
            expected_epoch: u.authorization_epoch,
            ...(operation === "active" ? { is_active: !u.is_active } : {}),
          },
          task.owner,
        ),
      (r) => {
        if (r.committed) void load();
      },
    );
  const templateState = (t: CanonicalTemplate) =>
    task.run(
      () =>
        authzCommand(
          `/role-templates/${encodeURIComponent(t.code)}/${t.is_active ? "archive" : "restore"}`,
          "post",
          { ...envelope, expected_version: t.version },
          task.owner,
        ),
      (r) => {
        if (r.committed) void load();
      },
    );
  const columns = [
    { title: "账号", dataIndex: "username" },
    { title: "显示名", dataIndex: "display_name" },
    {
      title: "规范授权来源",
      key: "source",
      render: (_: unknown, u: CanonicalAccount) => (
        <Space wrap>
          <Tag>
            {u.authorization_summary.template_code
              ? `${u.authorization_summary.template_code} v${u.authorization_summary.template_version}`
              : "无规范模板"}
          </Tag>
          {u.authorization_summary.override_resources.length > 0 && (
            <Tag>
              {u.authorization_summary.override_resources.length}个模块个人调整
            </Tag>
          )}
          {u.authorization_summary.template_current_version !==
            u.authorization_summary.template_version &&
            u.authorization_summary.template_code && (
              <Tag color="orange">模板有新版本</Tag>
            )}
        </Space>
      ),
    },
    { title: "角色（安全属性）", dataIndex: "role" },
    {
      title: "状态",
      key: "active",
      render: (_: unknown, u: CanonicalAccount) =>
        u.is_active ? "启用" : "停用",
    },
    {
      title: "操作",
      key: "op",
      render: (_: unknown, u: CanonicalAccount) => (
        <Space wrap>
          {can("edit_account_profiles") && (
            <Button
              type="link"
              onClick={() => setCommand({ kind: "profile", account: u })}
            >
              资料
            </Button>
          )}
          {can("manage_authorization") && (
            <>
              <Button type="link" onClick={() => setAccountTarget(u.username)}>
                权限与范围
              </Button>
              <Button
                type="link"
                disabled={u.role === "admin"}
                onClick={() => setBulk({ targets: targets([u]) })}
              >
                应用模板
              </Button>
              <Button
                type="link"
                onClick={() => setCommand({ kind: "security", account: u })}
              >
                安全属性
              </Button>
            </>
          )}
          {can("reset_account_passwords") && (
            <Button
              type="link"
              onClick={() => setCommand({ kind: "password", account: u })}
            >
              重置密码
            </Button>
          )}
          {can("manage_account_lifecycle") && (
            <>
              <Popconfirm
                title={u.is_active ? "确认停用账号？" : "确认启用账号？"}
                onConfirm={() => lifecycle(u, "active")}
              >
                <Button type="link" danger={u.is_active}>
                  {u.is_active ? "停用" : "启用"}
                </Button>
              </Popconfirm>
              <Popconfirm
                title="确认删除账号？历史记录保留。"
                onConfirm={() => lifecycle(u, "delete")}
              >
                <Button type="link" danger>
                  删除
                </Button>
              </Popconfirm>
            </>
          )}
        </Space>
      ),
    },
  ];
  return (
    <>
      <PageHeader title="账号与权限中心" />
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="统一模块授权"
        description="账号与模板使用完整模块配置。尚未迁移的模块只读展示待实施，旧布尔改权入口已停用；资料、角色和显式应用模板分别操作。"
      />
      {task.error && (
        <Alert
          type="error"
          message={task.error}
          action={<Button onClick={load}>重新加载</Button>}
        />
      )}
      {!loaded ? (
        <Spin />
      ) : (
        <Tabs
          items={[
            {
              key: "accounts",
              label: `账号（${accounts.length}）`,
              children: (
                <>
                  <Space wrap style={{ marginBottom: 12 }}>
                    <Input.Search
                      aria-label="筛选账号"
                      placeholder="用户名或显示名"
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                    />
                    {can("create_accounts") && catalog && (
                      <Button
                        type="primary"
                        onClick={() => setCommand({ kind: "create" })}
                      >
                        新建账号
                      </Button>
                    )}
                    {can("manage_authorization") && catalog && (
                      <Button
                        disabled={!selected.length}
                        onClick={() =>
                          setBulk({
                            targets: targets(
                              accounts.filter((u) => selected.includes(u.id)),
                            ),
                          })
                        }
                      >
                        批量授权与模板
                      </Button>
                    )}
                  </Space>
                  <Table
                    rowKey="id"
                    size="small"
                    columns={columns}
                    dataSource={accounts.filter((u) =>
                      `${u.username} ${u.display_name || ""}`.includes(query),
                    )}
                    loading={task.busy}
                    scroll={{ x: 1000 }}
                    rowSelection={
                      can("manage_authorization")
                        ? {
                            selectedRowKeys: selected,
                            onChange: (ks) => setSelected(ks as number[]),
                            getCheckboxProps: (u) => ({
                              disabled: u.role === "admin",
                            }),
                          }
                        : undefined
                    }
                  />
                </>
              ),
            },
            ...(can("manage_templates") && catalog
              ? [
                  {
                    key: "templates",
                    label: `规范模板（${templates.length}）`,
                    children: (
                      <>
                        <Button
                          onClick={() => setTemplateTarget(null)}
                          style={{ marginBottom: 12 }}
                        >
                          新建或复制模板
                        </Button>
                        <Table
                          rowKey="code"
                          size="small"
                          dataSource={templates}
                          columns={[
                            { title: "模板", dataIndex: "name" },
                            { title: "版本", dataIndex: "version" },
                            {
                              title: "模块配置",
                              key: "modules",
                              render: (_: unknown, t: CanonicalTemplate) =>
                                `${t.authorization_summary.configured_resources.length} 个`,
                            },
                            {
                              title: "规范来源账号",
                              key: "usage",
                              render: (_: unknown, t: CanonicalTemplate) =>
                                t.authorization_summary.canonical_usage_count,
                            },
                            {
                              title: "状态",
                              key: "state",
                              render: (_: unknown, t: CanonicalTemplate) =>
                                t.is_active ? "启用" : "停用",
                            },
                            {
                              title: "操作",
                              key: "op",
                              render: (_: unknown, t: CanonicalTemplate) => (
                                <Space>
                                  <Button onClick={() => setTemplateTarget(t)}>
                                    {t.locked ? "查看" : "编辑"}
                                  </Button>
                                  <Button
                                    disabled={
                                      !t.is_active ||
                                      t.locked ||
                                      !accounts.some(
                                        (u) =>
                                          u.authorization_summary
                                            .template_code === t.code &&
                                          u.role !== "admin",
                                      )
                                    }
                                    onClick={() =>
                                      setBulk({
                                        targets: targets(
                                          accounts.filter(
                                            (u) =>
                                              u.authorization_summary
                                                .template_code === t.code &&
                                              u.role !== "admin",
                                          ),
                                        ),
                                        sync: t,
                                      })
                                    }
                                  >
                                    预览同步账号
                                  </Button>
                                  <Popconfirm
                                    title={
                                      t.is_active
                                        ? "停用模板不会改变已应用账号，确认？"
                                        : "恢复模板不会自动同步账号，确认？"
                                    }
                                    onConfirm={() => templateState(t)}
                                  >
                                    <Button
                                      disabled={
                                        t.locked || (t.is_system && t.is_active)
                                      }
                                    >
                                      {t.is_active ? "停用" : "恢复"}
                                    </Button>
                                  </Popconfirm>
                                </Space>
                              ),
                            },
                          ]}
                        />
                      </>
                    ),
                  },
                ]
              : []),
          ]}
        />
      )}
      {catalog && accountTarget && (
        <ModuleAccountDrawer
          key={accountTarget}
          username={accountTarget}
          catalog={catalog}
          onClose={() => setAccountTarget(null)}
          onSaved={() => void load()}
        />
      )}
      {catalog && templateTarget !== undefined && (
        <ModuleTemplateDrawer
          key={templateTarget?.code || "new"}
          catalog={catalog}
          template={templateTarget}
          templates={templates}
          onClose={() => setTemplateTarget(undefined)}
          onSaved={() => void load()}
        />
      )}
      {catalog && bulk && (
        <ModuleBulkModal
          key={JSON.stringify(bulk)}
          catalog={catalog}
          templates={templates}
          targets={bulk.targets}
          syncTemplate={bulk.sync}
          onClose={() => setBulk(null)}
          onDone={() => void load()}
        />
      )}
      {command && (
        <AccountCommandModal
          key={`${command.kind}:${command.account?.username || "new"}`}
          kind={command.kind}
          account={command.account}
          catalog={catalog || fallbackCatalog}
          templates={templates}
          onClose={() => setCommand(null)}
          onDone={() => void load()}
        />
      )}
    </>
  );
}
