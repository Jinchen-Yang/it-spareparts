import { useEffect, useState } from "react";
import { Alert, Button, Drawer, Input, Select, Space, Spin } from "antd";
import {
  authzCommand,
  getTemplateModules,
  type AuthzCatalog,
  type TemplateModules,
  type ResourceConfiguration,
} from "../../api/authz";
import type { CanonicalTemplate } from "../../api/accounts";
import ModuleGrantEditor, {
  bundleErrors,
  type ModuleBundle,
} from "./ModuleGrantEditor";
import { useAuthorizationTask } from "./useAuthorizationTask";
export default function ModuleTemplateDrawer({
  catalog,
  template,
  templates,
  onClose,
  onSaved,
}: {
  catalog: AuthzCatalog;
  template: CanonicalTemplate | null;
  templates: CanonicalTemplate[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const task = useAuthorizationTask(template?.code || "new-template");
  const [loaded, setLoaded] = useState<TemplateModules | null>(null),
    [name, setName] = useState(template?.name || ""),
    [description, setDescription] = useState(template?.description || ""),
    [copy, setCopy] = useState<string>(),
    [bundle, setBundle] = useState<ModuleBundle>({}),
    [dirty, setDirty] = useState<Record<string, ResourceConfiguration | null>>(
      {},
    );
  const load = () =>
    template
      ? task.run(
          () => getTemplateModules(template.code, task.owner, catalog),
          (d) => {
            setLoaded(d);
            setName(d.template.name);
            setDescription(d.template.description || "");
            setBundle(d.modules);
            setDirty({});
          },
        )
      : Promise.resolve();
  useEffect(() => {
    void load();
  }, []);
  const save = () => {
    if (!name.trim() || bundleErrors(catalog, bundle).length) return;
    const metadata = {
      name: name.trim(),
      description: description.trim() || null,
    };
    const copied = templates.find((t) => t.code === copy);
    const body = template
      ? {
          schema_version: 1,
          catalog_version: catalog.catalog_version,
          expected_version: loaded?.template.version,
          metadata,
          ...(Object.keys(dirty).length
            ? {
                updates: Object.entries(dirty).map(([resource_id, c]) =>
                  c
                    ? {
                        resource_id,
                        operation: "replace_resource",
                        configuration: c,
                      }
                    : { resource_id, operation: "remove_resource" },
                ),
              }
            : {}),
        }
      : {
          schema_version: 1,
          catalog_version: catalog.catalog_version,
          metadata,
          authorization: copied
            ? {
                kind: "copy_template",
                code: copied.code,
                expected_version: copied.version,
              }
            : { kind: "explicit", modules: bundle },
        };
    void task.run(
      () =>
        authzCommand(
          template
            ? `/role-templates/${encodeURIComponent(template.code)}`
            : "/role-templates",
          template ? "put" : "post",
          body,
          task.owner,
        ),
      (r) => {
        if (r.committed) {
          onSaved();
          onClose();
        }
      },
    );
  };
  return (
    <Drawer
      open
      width={780}
      title={template ? `规范模板 · ${template.name}` : "新建规范模板"}
      onClose={onClose}
      extra={
        <Button
          type="primary"
          loading={task.busy}
          disabled={
            !name.trim() ||
            !!bundleErrors(catalog, bundle).length ||
            (!!template && !loaded) ||
            template?.locked
          }
          onClick={save}
        >
          仅保存模板
        </Button>
      }
    >
      {task.error && (
        <Alert
          type="error"
          message={task.error}
          action={
            template ? <Button onClick={load}>重新加载</Button> : undefined
          }
        />
      )}
      {template && !loaded ? (
        <Spin />
      ) : (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Alert
            type="info"
            message="保存模板不会改变任何账号"
            description="保存后请单独选择账号并预览同步。未迁模块、旧角色和人员归属不会由模板修改。"
          />
          <label>
            模板名称
            <Input
              value={name}
              disabled={task.busy || template?.locked}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label>
            说明
            <Input.TextArea
              value={description}
              disabled={task.busy || template?.locked}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          {template && <p>兼容角色标签（只读）：{template.base_role}</p>}
          {!template && (
            <Select
              allowClear
              aria-label="复制规范模板"
              placeholder="不复制，明确配置初始包"
              value={copy}
              disabled={task.busy}
              onChange={setCopy}
              options={templates
                .filter((t) => t.is_active && !t.locked)
                .map((t) => ({
                  value: t.code,
                  label: `复制 ${t.name} v${t.version}`,
                }))}
            />
          )}
          {!copy && (
            <ModuleGrantEditor
              catalog={catalog}
              value={bundle}
              disabled={task.busy || template?.locked}
              onChange={(id, c) => {
                setDirty((d) => ({ ...d, [id]: c }));
                setBundle((b) => {
                  const n = { ...b };
                  if (c) n[id] = c;
                  else delete n[id];
                  return n;
                });
              }}
            />
          )}
        </Space>
      )}
    </Drawer>
  );
}
