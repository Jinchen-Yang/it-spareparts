import { useState } from "react";
import { Alert, Button, Checkbox, Input, Modal, Select, Space } from "antd";
import {
  authzCommand,
  type AuthzCatalog,
  type AuthorizationPreview,
} from "../../api/authz";
import type { CanonicalAccount, CanonicalTemplate } from "../../api/accounts";
import ModuleGrantEditor, {
  bundleErrors,
  type ModuleBundle,
} from "./ModuleGrantEditor";
import CustomerAssignmentEditor from "./CustomerAssignmentEditor";
import AuthorizationDiff from "./AuthorizationDiff";
import { useAuthorizationTask } from "./useAuthorizationTask";
export type AccountCommandKind = "create" | "profile" | "security" | "password";
export default function AccountCommandModal({
  kind,
  account,
  catalog,
  templates,
  onClose,
  onDone,
}: {
  kind: AccountCommandKind;
  account?: CanonicalAccount;
  catalog: AuthzCatalog;
  templates: CanonicalTemplate[];
  onClose: () => void;
  onDone: () => void;
}) {
  const task = useAuthorizationTask(account?.username || "new-account");
  const [username, setUsername] = useState(""),
    [name, setName] = useState(account?.display_name || ""),
    [password, setPassword] = useState(""),
    [role, setRole] = useState(account?.role || "readonly"),
    [salesperson, setSalesperson] = useState(account?.salesperson_name || ""),
    [setMapping, setSetMapping] = useState(false),
    [admin, setAdmin] = useState(false),
    [authorization, setAuthorization] = useState("none"),
    [templateCode, setTemplateCode] = useState<string>(),
    [bundle, setBundle] = useState<ModuleBundle>({}),
    [ids, setIds] = useState<number[]>([]),
    [preview, setPreview] = useState<AuthorizationPreview | null>(null);
  const dirty = () => {
    setPreview(null);
    task.invalidate();
  };
  const selected = templates.find((t) => t.code === templateCode);
  const security =
    kind === "security" || (kind === "create" && (admin || setMapping));
  const valid =
    kind === "create"
      ? !!username.trim() &&
        password.length >= 6 &&
        (authorization !== "apply_template" || !!selected) &&
        (authorization !== "explicit" || !bundleErrors(catalog, bundle).length)
      : kind === "password"
        ? password.length >= 6
        : true;
  const path =
    kind === "create"
      ? "/accounts"
      : `/accounts/${encodeURIComponent(account!.username)}${kind === "password" ? "/password" : ""}`;
  const body = (): Record<string, unknown> => {
    const base = {
      schema_version: 1,
      catalog_version: catalog.catalog_version,
    };
    if (kind === "profile")
      return { ...base, profile: { display_name: name.trim() || null } };
    if (kind === "password")
      return {
        ...base,
        expected_epoch: account!.authorization_epoch,
        password,
      };
    if (kind === "security")
      return {
        ...base,
        expected_epoch: account!.authorization_epoch,
        security_change: { role, salesperson_name: salesperson.trim() || null },
      };
    return {
      ...base,
      profile: {
        username: username.trim(),
        display_name: name.trim() || null,
        ...(!security ? { password } : {}),
      },
      authorization:
        authorization === "none"
          ? { kind: "none" }
          : authorization === "apply_template"
            ? {
                kind: "apply_template",
                template_code: selected!.code,
                expected_template_version: selected!.version,
              }
            : { kind: "explicit", modules: bundle },
      ...(ids.length ? { customer_assignments: { ids } } : {}),
      ...(security
        ? {
            security_change: {
              ...(admin ? { role: "admin" } : {}),
              ...(setMapping
                ? { salesperson_name: salesperson.trim() || null }
                : {}),
            },
          }
        : {}),
    };
  };
  const send = () => {
    if (!valid) return;
    setPreview(null);
    void task.run(
      () =>
        authzCommand(
          path,
          kind === "create" ? "post" : "put",
          { ...body(), ...(security ? { dry_run: true } : {}) },
          task.owner,
        ),
      (r) => {
        if (r.committed) {
          onDone();
          onClose();
        } else setPreview(r);
      },
    );
  };
  const execute = () => {
    if (!preview || !valid) return;
    const frozen = preview;
    setPreview(null);
    const command = {
      ...frozen.command,
      dry_run: false,
      fingerprint: frozen.fingerprint,
    };
    if (kind === "create")
      Object.assign(command, {
        profile: {
          ...(frozen.command.profile as Record<string, unknown>),
          password,
        },
      });
    void task.run(
      () =>
        authzCommand(
          path,
          kind === "create" ? "post" : "put",
          command,
          task.owner,
        ),
      (r) => {
        if (r.committed) {
          onDone();
          onClose();
        }
      },
    );
  };
  const title = {
    create: "新建账号",
    profile: "修改显示资料",
    security: "修改角色与销售归属",
    password: "重置账号密码",
  }[kind];
  return (
    <Modal
      open
      title={`${title}${account ? " · " + account.username : ""}`}
      width={760}
      onCancel={onClose}
      footer={null}
    >
      <Space direction="vertical" style={{ width: "100%" }}>
        {task.error && (
          <Alert
            type="error"
            message={task.error}
            description="预览已失效；冲突不会自动重试。"
          />
        )}
        {(kind === "create" || kind === "profile") && (
          <>
            {kind === "create" && (
              <label>
                用户名
                <Input
                  value={username}
                  disabled={task.busy}
                  onChange={(e) => {
                    dirty();
                    setUsername(e.target.value);
                  }}
                  autoComplete="off"
                />
              </label>
            )}
            <label>
              显示名
              <Input
                value={name}
                disabled={task.busy}
                onChange={(e) => {
                  dirty();
                  setName(e.target.value);
                }}
              />
            </label>
          </>
        )}
        {(kind === "create" || kind === "password") && (
          <label>
            密码（至少6位）
            <Input.Password
              value={password}
              disabled={task.busy}
              onChange={(e) => {
                dirty();
                setPassword(e.target.value);
              }}
              autoComplete="new-password"
            />
          </label>
        )}
        {kind === "profile" && (
          <Alert
            type="info"
            message="此表单只保存显示名"
            description={`角色 ${account?.role} 与销售归属只读；授权配置和模板来源保持。`}
          />
        )}
        {kind === "security" && (
          <>
            <Alert
              type="warning"
              message="角色与销售归属属于安全属性"
              description="必须先预览；角色可能影响尚未迁移模块。不会自动创建新业务授权。"
            />
            <label>
              角色
              <Select
                value={role}
                disabled={task.busy}
                onChange={(v) => {
                  dirty();
                  setRole(v);
                }}
                style={{ width: "100%" }}
                options={[
                  "readonly",
                  "sales",
                  "purchaser",
                  "boss",
                  "maintenance_manager",
                  "admin",
                ].map((value) => ({ value, label: value }))}
              />
            </label>
            <label>
              销售数据姓名
              <Input
                value={salesperson}
                disabled={task.busy}
                onChange={(e) => {
                  dirty();
                  setSalesperson(e.target.value);
                }}
              />
            </label>
          </>
        )}
        {kind === "create" && (
          <>
            <Alert
              type="info"
              message="新账号默认无模块授权，角色为readonly"
              description="选择授权时与建号原子提交；模板不复制客户或项目归属。"
            />
            <Select
              aria-label="初始模块授权"
              value={authorization}
              disabled={task.busy}
              style={{ width: "100%" }}
              onChange={(v) => {
                dirty();
                setAuthorization(v);
              }}
              options={[
                { value: "none", label: "无模块授权" },
                { value: "explicit", label: "明确配置模块" },
                { value: "apply_template", label: "显式应用规范模板" },
              ]}
            />
            {authorization === "explicit" && (
              <ModuleGrantEditor
                catalog={catalog}
                value={bundle}
                disabled={task.busy}
                onChange={(id, c) => {
                  dirty();
                  setBundle((b) => {
                    const n = { ...b };
                    if (c) n[id] = c;
                    else delete n[id];
                    return n;
                  });
                }}
              />
            )}
            {authorization === "apply_template" && (
              <Select
                aria-label="初始规范模板"
                value={templateCode}
                disabled={task.busy}
                style={{ width: "100%" }}
                onChange={(v) => {
                  dirty();
                  setTemplateCode(v);
                }}
                options={templates
                  .filter((t) => t.is_active && !t.locked)
                  .map((t) => ({
                    value: t.code,
                    label: `${t.name} v${t.version}`,
                  }))}
              />
            )}
            {catalog.modules.some(
              (m) => m.id === "sales_analysis" && m.configurable,
            ) && (
              <CustomerAssignmentEditor
                owner={task.owner}
                selected={ids}
                customers={[]}
                disabled={task.busy}
                onConfirm={(v) => {
                  dirty();
                  setIds(v);
                }}
              />
            )}
            <Checkbox
              checked={admin}
              disabled={task.busy}
              onChange={(e) => {
                dirty();
                setAdmin(e.target.checked);
              }}
            >
              明确创建管理员（需要安全预览）
            </Checkbox>
            <Checkbox
              checked={setMapping}
              disabled={task.busy}
              onChange={(e) => {
                dirty();
                setSetMapping(e.target.checked);
              }}
            >
              设置初始销售归属（需要安全预览）
            </Checkbox>
            {setMapping && (
              <label>
                销售数据姓名
                <Input
                  value={salesperson}
                  disabled={task.busy}
                  onChange={(e) => {
                    dirty();
                    setSalesperson(e.target.value);
                  }}
                />
              </label>
            )}
          </>
        )}
        {preview && <AuthorizationDiff preview={preview} catalog={catalog} />}
        <Space>
          <Button onClick={onClose}>取消</Button>
          <Button
            type="primary"
            disabled={!valid}
            loading={task.busy}
            onClick={send}
          >
            {security
              ? "生成安全变更预览"
              : kind === "create"
                ? "原子创建账号"
                : "保存"}
          </Button>
          {security && (
            <Button
              danger
              disabled={!preview}
              loading={task.busy}
              onClick={execute}
            >
              确认执行预览
            </Button>
          )}
        </Space>
      </Space>
    </Modal>
  );
}
