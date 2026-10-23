import { useEffect, useState } from "react";
import { Alert, Button, Drawer, Space, Spin, Tag } from "antd";
import {
  authzCommand,
  denialConfiguration,
  getAccountModules,
  type AccountModules,
  type AuthzCatalog,
  type ResourceConfiguration,
  type ResourceOperation,
} from "../../api/authz";
import ModuleGrantEditor, {
  bundleErrors,
  type ModuleBundle,
} from "./ModuleGrantEditor";
import CustomerAssignmentEditor from "./CustomerAssignmentEditor";
import { useAuthorizationTask } from "./useAuthorizationTask";
export default function ModuleAccountDrawer({
  username,
  catalog,
  onClose,
  onSaved,
}: {
  username: string;
  catalog: AuthzCatalog;
  onClose: () => void;
  onSaved: () => void;
}) {
  const task = useAuthorizationTask(username);
  const [data, setData] = useState<AccountModules | null>(null),
    [bundle, setBundle] = useState<ModuleBundle>({}),
    [ops, setOps] = useState<Record<string, ResourceOperation>>({}),
    [ids, setIds] = useState<number[]>([]),
    [customerChanged, setCustomerChanged] = useState(false);
  const load = () =>
    task.run(
      () => getAccountModules(username, task.owner, catalog),
      (d) => {
        setData(d);
        setBundle(
          Object.fromEntries(
            Object.entries(d.resources).flatMap(([id, r]) =>
              r.configuration ? [[id, r.configuration]] : [],
            ),
          ),
        );
        setIds(d.customer_assignments.ids);
        setOps({});
        setCustomerChanged(false);
      },
    );
  useEffect(() => {
    void load();
  }, []);
  const change = (id: string, c: ResourceConfiguration | null) => {
    const m = catalog.modules.find((x) => x.id === id);
    if (!data || !m || !m.configurable) return;
    const next = c || denialConfiguration(m);
    setBundle((b) => ({ ...b, [id]: next }));
    setOps((o) => ({
      ...o,
      [id]: {
        resource_id: id,
        expected_version: data.resources[id]?.version ?? 0,
        operation: c ? "replace_resource" : "revoke_resource",
        configuration: next,
      },
    }));
  };
  const errors = bundleErrors(catalog, bundle);
  const save = () => {
    if (!data || !Object.keys(ops).length || errors.length) return;
    void task.run(
      () =>
        authzCommand(
          `/authz/users/${encodeURIComponent(username)}/modules`,
          "put",
          {
            schema_version: 1,
            catalog_version: catalog.catalog_version,
            expected_epoch: data.authorization_epoch,
            updates: Object.values(ops),
            ...(customerChanged ? { customer_assignments: { ids } } : {}),
          },
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
      title={`权限与范围 · ${username}`}
      width={780}
      onClose={onClose}
      extra={
        <Button
          type="primary"
          loading={task.busy}
          disabled={!data || !Object.keys(ops).length || !!errors.length}
          onClick={save}
        >
          保存配置与归属
        </Button>
      }
    >
      {task.error && (
        <Alert
          type="error"
          message={task.error}
          description="草稿保留；版本冲突后需要重新加载，确认新配置后再保存。"
          action={<Button onClick={load}>重新加载</Button>}
        />
      )}
      {!data ? (
        <Spin />
      ) : (
        <Space direction="vertical" size="large" style={{ width: "100%" }}>
          <Alert
            type="info"
            message="每个模块的个人调整会完整替换该模块配置"
            description="模板来源与客户归属独立；角色分类不通过此表单修改。待实施模块的旧配置保持只读。"
          />
          <p>
            授权来源：
            {data.template
              ? `${data.template.code} v${data.template.version}${data.template.stale ? "（模板有新版本，尚未同步）" : ""}`
              : "未应用规范模板"}
          </p>
          {Object.entries(data.resources).map(([id, r]) => {
            const m = catalog.modules.find((x) => x.id === id);
            return m?.configurable ? (
              <div key={id}>
                <b>{m.label}</b>{" "}
                <Tag>
                  {r.personal_override.present
                    ? "存在个人调整"
                    : "来自保存模板或无配置"}
                </Tag>
                <Button
                  size="small"
                  disabled={task.busy || !r.personal_override.present}
                  onClick={() => {
                    setOps((o) => ({
                      ...o,
                      [id]: {
                        resource_id: id,
                        expected_version: r.version,
                        operation: "clear_override",
                      },
                    }));
                    const c =
                      r.template_snapshot?.configuration ||
                      denialConfiguration(m);
                    setBundle((b) => ({ ...b, [id]: c }));
                  }}
                >
                  {r.template_snapshot?.configuration
                    ? "恢复保存的模板配置"
                    : "清除个人调整并撤销模块"}
                </Button>
                {ops[id]?.operation === "clear_override" && (
                  <span>
                    {r.template_snapshot?.configuration
                      ? "保存后清除整资源个人调整；不会追随最新模板。"
                      : "没有保存的模板配置；清除个人调整后该模块将拒绝访问。"}
                  </span>
                )}
              </div>
            ) : null;
          })}
          <ModuleGrantEditor
            catalog={catalog}
            value={bundle}
            onChange={change}
            disabled={task.busy}
          />
          {catalog.modules.some(
            (m) => m.id === "sales_analysis" && m.configurable,
          ) && (
            <section>
              <h3>负责客户</h3>
              <p>
                与本次销售配置原子保存，覆盖客户全部历史和后续订单。修改客户归属将明确保存销售模块的完整个人调整。
              </p>
              <CustomerAssignmentEditor
                owner={task.owner}
                selected={ids}
                customers={data.customer_assignments.customers}
                disabled={task.busy}
                onConfirm={(next) => {
                  setIds(next);
                  setCustomerChanged(true);
                  const m = catalog.modules.find(
                    (m) => m.id === "sales_analysis",
                  )!;
                  change(m.id, bundle[m.id] || denialConfiguration(m));
                }}
              />
            </section>
          )}
          <p>
            项目范围来自现有项目负责人关系；请在项目管理流程中维护。此处不复制或修改项目关系。
          </p>
        </Space>
      )}
    </Drawer>
  );
}
