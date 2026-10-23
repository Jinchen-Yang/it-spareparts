import { Alert, Card, Descriptions, Space, Tag } from "antd";
import type {
  AuthorizationPreview,
  AuthzCatalog,
  ResourceConfiguration,
} from "../../api/authz";
export default function AuthorizationDiff({
  preview,
  catalog,
}: {
  preview: AuthorizationPreview;
  catalog: AuthzCatalog;
}) {
  const conf = (id: string, c: ResourceConfiguration | null) => {
    if (!c) return "无配置";
    const m = catalog.modules.find((x) => x.id === id);
    return `动作：${c.actions.map((a) => m?.actions.find((x) => x.id === a)?.label || a).join("、") || "拒绝"}；范围：${m?.scopes.find((s) => s.id === c.scope)?.label || c.scope}；字段：${c.field_permissions.map((f) => m?.field_permissions.find((x) => x.id === f)?.label || f).join("、") || "无"}`;
  };
  if (!Array.isArray(preview.preview)) {
    const p = preview.preview;
    const profile = preview.command.profile as
      | { username?: string; display_name?: string }
      | undefined;
    if (profile) {
      const security = preview.command.security_change as
        | { role?: string; salesperson_name?: string | null }
        | undefined;
      const authorization = preview.command.authorization as
        | {
            kind?: string;
            template_code?: string;
            expected_template_version?: number;
            modules?: Record<string, ResourceConfiguration>;
          }
        | undefined;
      const customers = preview.command.customer_assignments as
        | { ids?: number[] }
        | undefined;
      return (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Alert
            type="warning"
            message="确认创建新的安全身份"
            description="执行时才提交本地密码；账号、归属与授权在一个事务中创建。管理员身份不会自动获得业务模块权限。"
          />
          <Descriptions
            column={1}
            items={[
              {
                key: "username",
                label: "新用户名",
                children: profile.username,
              },
              {
                key: "display",
                label: "显示名",
                children: profile.display_name || "未设置",
              },
              {
                key: "role",
                label: "新账号角色",
                children: security?.role === "admin" ? "管理员" : "readonly",
              },
              {
                key: "salesperson",
                label: "销售数据姓名",
                children: security?.salesperson_name || "未设置",
              },
              {
                key: "authorization",
                label: "初始授权",
                children:
                  authorization?.kind === "apply_template"
                    ? `模板 ${authorization.template_code} v${authorization.expected_template_version}`
                    : authorization?.kind === "explicit"
                      ? "明确模块配置"
                      : "无模块授权",
              },
              {
                key: "customers",
                label: "明确负责客户ID",
                children: customers?.ids?.join("、") || "无",
              },
            ]}
          />
          {Object.entries(authorization?.modules || {}).map(([id, c]) => (
            <p key={id}>
              {catalog.modules.find((m) => m.id === id)?.label || id}：
              {conf(id, c)}
            </p>
          ))}
        </Space>
      );
    }
    const before = p.security_before || {},
      after = p.security_after || {};
    return (
      <Space direction="vertical" style={{ width: "100%" }}>
        <Alert
          type="warning"
          message="安全属性变更需确认"
          description={
            p.legacy_role_behavior_may_change
              ? "角色变更可能改变尚未迁移模块的行为；新业务授权不会自动增加。"
              : "变更后相关账号需要重新登录。"
          }
        />
        <Descriptions
          column={1}
          items={[
            {
              key: "target",
              label: "账号",
              children: p.subject?.username || "当前目标",
            },
            {
              key: "role",
              label: "角色",
              children: `${String(before.role ?? "未设置")} → ${String(after.role ?? "未设置")}`,
            },
            {
              key: "salesperson",
              label: "销售数据姓名",
              children: `${String(before.salesperson_name ?? "未设置")} → ${String(after.salesperson_name ?? "未设置")}`,
            },
          ]}
        />
      </Space>
    );
  }
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Alert
        type="warning"
        message={`将原子处理 ${preview.affected ?? preview.preview.length} 个账号`}
        description="模板来源、个人调整、范围与字段均按以下结果执行。任何冲突都会使整批失败。"
      />
      {preview.preview.map((p) => (
        <Card
          key={p.subject.id}
          size="small"
          title={p.subject.display_name || p.subject.username}
        >
          <p>
            模板：
            {p.template_before
              ? `${p.template_before.code} v${p.template_before.version}`
              : "无"}{" "}
            →{" "}
            {p.template_after
              ? `${p.template_after.code} v${p.template_after.version}`
              : "无"}
          </p>
          {p.source_changed && !p.effective_changed && (
            <Tag>仅授权来源变化，配置数值相同</Tag>
          )}
          {p.session_revoked && <Tag color="orange">旧登录将失效</Tag>}
          {p.resources.map((r) => (
            <section key={r.resource_id}>
              <b>
                {catalog.modules.find((m) => m.id === r.resource_id)?.label ||
                  r.resource_id}
              </b>
              <p>变更前：{conf(r.resource_id, r.configuration_before)}</p>
              <p>变更后：{conf(r.resource_id, r.configuration_after)}</p>
              <p>
                个人调整：{r.override_before.present ? "存在" : "无"} →{" "}
                {r.override_after.present ? "存在" : "无"}（相同值也保留来源）
              </p>
            </section>
          ))}
        </Card>
      ))}
    </Space>
  );
}
