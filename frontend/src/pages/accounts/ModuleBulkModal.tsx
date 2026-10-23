import { useState } from "react";
import { Alert, Button, Checkbox, Modal, Select, Space } from "antd";
import {
  authzCommand,
  type AuthzCatalog,
  type AuthorizationPreview,
  type BulkCommandOperation,
  type Target,
} from "../../api/authz";
import type { CanonicalTemplate } from "../../api/accounts";
import ModuleGrantEditor, {
  bundleErrors,
  type ModuleBundle,
} from "./ModuleGrantEditor";
import AuthorizationDiff from "./AuthorizationDiff";
import { useAuthorizationTask } from "./useAuthorizationTask";
export default function ModuleBulkModal({
  catalog,
  templates,
  targets,
  syncTemplate,
  onClose,
  onDone,
}: {
  catalog: AuthzCatalog;
  templates: CanonicalTemplate[];
  targets: Target[];
  syncTemplate?: CanonicalTemplate;
  onClose: () => void;
  onDone: () => void;
}) {
  const task = useAuthorizationTask(JSON.stringify(targets));
  const [kind, setKind] = useState<BulkCommandOperation["kind"]>(
      syncTemplate ? "sync_template" : "apply_template",
    ),
    [code, setCode] = useState(syncTemplate?.code),
    [clear, setClear] = useState(false),
    [bundle, setBundle] = useState<ModuleBundle>({}),
    [resourceIds, setResourceIds] = useState<string[]>([]),
    [preview, setPreview] = useState<AuthorizationPreview | null>(null);
  const dirty = () => {
    task.invalidate();
    setPreview(null);
  };
  const tpl = templates.find((t) => t.code === code);
  const operation: BulkCommandOperation | null =
    kind === "apply_template" || kind === "sync_template"
      ? tpl
        ? {
            kind,
            template_code: tpl.code,
            expected_template_version: tpl.version,
            override_policy: clear ? "clear" : "preserve",
          }
        : null
      : kind === "replace_resources"
        ? {
            kind,
            updates: Object.entries(bundle).map(
              ([resource_id, configuration]) => ({
                resource_id,
                configuration,
              }),
            ),
          }
        : { kind, resource_ids: resourceIds };
  const valid =
    !!targets.length &&
    !!operation &&
    (kind === "apply_template" ||
      kind === "sync_template" ||
      (kind === "replace_resources"
        ? Object.keys(bundle).length > 0 &&
          !bundleErrors(catalog, bundle).length
        : resourceIds.length > 0));
  const path = syncTemplate
    ? `/authz/templates/${encodeURIComponent(syncTemplate.code)}/sync`
    : "/accounts/bulk";
  const makePreview = () => {
    if (!valid) return;
    setPreview(null);
    void task.run(
      () =>
        authzCommand(
          path,
          "post",
          {
            schema_version: 1,
            catalog_version: catalog.catalog_version,
            targets,
            operation,
            dry_run: true,
          },
          task.owner,
        ),
      (r) => {
        if (!r.committed) setPreview(r);
      },
    );
  };
  const execute = () => {
    if (!preview) return;
    const frozen = preview;
    setPreview(null);
    void task.run(
      () =>
        authzCommand(
          path,
          "post",
          {
            ...frozen.command,
            dry_run: false,
            fingerprint: frozen.fingerprint,
          },
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
  return (
    <Modal
      open
      width={820}
      title={syncTemplate ? "同步规范模板" : "应用模板与批量授权"}
      onCancel={onClose}
      footer={null}
    >
      <Space direction="vertical" style={{ width: "100%" }}>
        <Alert
          type="info"
          message={`明确选择了 ${targets.length} 个账号`}
          description="客户与项目归属不会从模板复制；执行前展示范围、字段及来源变化。"
        />
        {task.error && (
          <Alert
            type="error"
            message={task.error}
            description="本次预览已失效；不会自动重新执行。"
          />
        )}
        {!syncTemplate && (
          <Select
            aria-label="授权操作"
            value={kind}
            disabled={task.busy}
            style={{ width: "100%" }}
            onChange={(v) => {
              dirty();
              setKind(v);
            }}
            options={[
              { value: "apply_template", label: "显式应用模板" },
              { value: "replace_resources", label: "替换完整模块配置" },
              { value: "revoke_resources", label: "撤销模块授权" },
              {
                value: "clear_overrides",
                label: "清除个人调整（无模板配置则撤销）",
              },
            ]}
          />
        )}
        {(kind === "apply_template" || kind === "sync_template") && (
          <>
            <Select
              aria-label="规范模板"
              value={code}
              disabled={task.busy || !!syncTemplate}
              style={{ width: "100%" }}
              onChange={(v) => {
                dirty();
                setCode(v);
              }}
              options={templates
                .filter((t) => t.is_active && !t.locked)
                .map((t) => ({
                  value: t.code,
                  label: `${t.name} v${t.version}`,
                }))}
            />
            <Checkbox
              disabled={task.busy}
              checked={clear}
              onChange={(e) => {
                dirty();
                setClear(e.target.checked);
              }}
            >
              清除整资源个人调整（默认保留）
            </Checkbox>
          </>
        )}
        {kind === "replace_resources" && (
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
        {(kind === "revoke_resources" || kind === "clear_overrides") && (
          <Checkbox.Group
            disabled={task.busy}
            value={resourceIds}
            options={catalog.modules
              .filter((m) => m.configurable)
              .map((m) => ({ value: m.id, label: m.label }))}
            onChange={(v) => {
              dirty();
              setResourceIds(v as string[]);
            }}
          />
        )}
        {preview && <AuthorizationDiff preview={preview} catalog={catalog} />}
        <Space>
          <Button onClick={onClose}>取消</Button>
          <Button loading={task.busy} disabled={!valid} onClick={makePreview}>
            生成变更预览
          </Button>
          <Button
            type="primary"
            danger
            loading={task.busy}
            disabled={!preview}
            onClick={execute}
          >
            确认执行预览
          </Button>
        </Space>
      </Space>
    </Modal>
  );
}
