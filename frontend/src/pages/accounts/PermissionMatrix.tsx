/** 权限矩阵（权限中心 v2 核心组件）——账号编辑 / 模板编辑 / 只读预览三处复用。
 *
 * 能力：五分组卡片、每键业务语言（一句话+详情 Popover 八要素）、敏感级标记、
 * 来源标记（模板开·模板关·单独开启·单独关闭·随依赖开启）、搜索、分组全选/清空、
 * 只看已选、依赖自动补齐 + 关闭破坏依赖时的"为什么不可用"提示、模板对比列。
 * 客户端校验只为即时反馈——后端 combo_errors / 高风险守护是最终裁判。
 * 响应式：网格 auto-fill minmax，1440px 三列 / 390px 单列。
 */
import { useMemo, useState } from "react";
import { Alert, Checkbox, Empty, Input, Popover, Space, Switch, Tag, Typography } from "antd";
import { InfoCircleOutlined, SearchOutlined } from "@ant-design/icons";
import type { AccountsMeta, Perms } from "../../api/accounts";
import { comboErrors, dependentActions, missingDeps } from "../../api/accounts";

const SENSITIVITY: Record<string, { color: string; text: string }> = {
  low: { color: "default", text: "低敏感" },
  medium: { color: "blue", text: "中敏感" },
  high: { color: "orange", text: "高敏感" },
  critical: { color: "red", text: "极高" },
};

export interface PermissionMatrixProps {
  meta: AccountsMeta;
  /** 当前编辑的最终权限图 */
  value: Perms;
  /** 模板快照底座（账号模式给；模板编辑模式不给 → 不画来源标记） */
  base?: Perms;
  /** 对比列（如"模板现值"），只读展示 */
  compare?: { label: string; perms: Perms };
  /** 不传 = 只读矩阵 */
  onChange?: (next: Perms) => void;
  /** 高风险键禁用并说明原因（非管理员操作者） */
  lockHighRisk?: boolean;
}

export default function PermissionMatrix({
  meta, value, base, compare, onChange, lockHighRisk,
}: PermissionMatrixProps) {
  const [search, setSearch] = useState("");
  const [onlyChecked, setOnlyChecked] = useState(false);
  const [autoKeys, setAutoKeys] = useState<Set<string>>(new Set());
  const readonly = !onChange;

  const errors = useMemo(() => comboErrors(value, meta), [value, meta]);

  const matches = (k: string) => {
    if (onlyChecked && !value[k]) return false;
    if (!search.trim()) return true;
    const m = meta.meta[k];
    const hay = `${k} ${m?.label || ""} ${m?.summary || ""} ${meta.labels[k] || ""}`.toLowerCase();
    return hay.includes(search.trim().toLowerCase());
  };

  const toggle = (k: string, checked: boolean) => {
    if (!onChange) return;
    const next = { ...value, [k]: checked };
    const auto = new Set(autoKeys);
    if (checked) {
      // 依赖自动补齐：开动作把缺的"看"一起带上，并打"随依赖开启"标记
      for (const dep of missingDeps(k, next, meta)) {
        next[dep] = true;
        auto.add(dep);
      }
      auto.delete(k);
    } else {
      auto.delete(k);
    }
    setAutoKeys(auto);
    onChange(next);
  };

  const sourceTag = (k: string) => {
    if (!base) return null;
    const b = !!base[k];
    const v = !!value[k];
    if (autoKeys.has(k) && v && !b) return <Tag color="geekblue" style={{ marginInlineStart: 4 }}>随依赖开启</Tag>;
    if (v && !b) return <Tag color="green" style={{ marginInlineStart: 4 }}>单独开启</Tag>;
    if (!v && b) return <Tag color="orange" style={{ marginInlineStart: 4 }}>单独关闭</Tag>;
    return null;
  };

  const whyDisabled = (k: string): string | null => {
    if (readonly) return null;
    if (lockHighRisk && meta.high_risk_keys.includes(k)) {
      return "高风险权限：只有管理员本人可以授予或撤销";
    }
    return null;
  };

  const detail = (k: string) => {
    const m = meta.meta[k];
    if (!m) return null;
    const deps = [meta.dependencies.action_data[k], meta.dependencies.action_page[k]].filter(Boolean) as string[];
    const dependents = dependentActions(k, value, meta);
    return (
      <div style={{ maxWidth: 340 }}>
        <p style={{ margin: "0 0 6px" }}>{m.summary}</p>
        <p style={{ margin: "0 0 4px" }}><b>能看/能做：</b>{m.can}</p>
        <p style={{ margin: "0 0 4px" }}><b>不能看/不能做：</b>{m.cannot}</p>
        <p style={{ margin: "0 0 4px" }}><b>典型岗位：</b>{(m.typical || []).join("、")}</p>
        {deps.length > 0 && (
          <p style={{ margin: "0 0 4px" }}>
            <b>依赖：</b>{deps.map((d) => meta.meta[d]?.label || d).join("、")}（勾选本项会自动带上）
          </p>
        )}
        {dependents.length > 0 && (
          <p style={{ margin: "0 0 4px", color: "#d46b08" }}>
            <b>关闭影响：</b>已开启的「{dependents.map((d) => meta.meta[d]?.label || d).join("、")}」依赖本项，关掉会被系统拒绝保存
          </p>
        )}
        <p style={{ margin: 0, color: "#cf1322" }}><b>风险：</b>{m.risk}</p>
      </div>
    );
  };

  return (
    <div>
      <Space wrap style={{ marginBottom: 12, width: "100%", justifyContent: "space-between" }}>
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder="搜索权限（名称/说明）"
          style={{ width: 240 }}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <Space size={4}>
          <Switch size="small" checked={onlyChecked} onChange={setOnlyChecked} />
          <span style={{ fontSize: 13, color: "#666" }}>只看已开启</span>
        </Space>
      </Space>

      {errors.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="权限组合不完整，保存会被拒绝"
          description={<ul style={{ margin: 0, paddingInlineStart: 18 }}>{errors.map((e) => <li key={e}>{e}</li>)}</ul>}
        />
      )}

      {meta.groups.map((g) => {
        const keys = g.keys.filter(matches);
        if (keys.length === 0) return null;
        const allOn = g.keys.every((k) => value[k]);
        return (
          <section key={g.key} style={{
            border: "1px solid var(--ant-color-border, #f0f0f0)", borderRadius: 8,
            padding: "10px 12px", marginBottom: 12,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 4 }}>
              <Space size={8} wrap>
                <b style={{ fontSize: 14 }}>{g.label}</b>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>{g.hint}</Typography.Text>
              </Space>
              {!readonly && g.key !== "admin" && (
                <Space size={8}>
                  <a style={{ fontSize: 12 }} onClick={() => {
                    if (!onChange) return;
                    const next = { ...value };
                    g.keys.forEach((k) => { if (!whyDisabled(k)) next[k] = !allOn; });
                    onChange(next);
                  }}>{allOn ? "全部关闭" : "全部开启"}</a>
                </Space>
              )}
            </div>
            <div style={{
              display: "grid", gap: "6px 16px", marginTop: 8,
              gridTemplateColumns: "repeat(auto-fill, minmax(270px, 1fr))",
            }}>
              {keys.map((k) => {
                const m = meta.meta[k];
                const sens = SENSITIVITY[m?.sensitivity || "low"];
                const disabledReason = whyDisabled(k);
                const row = (
                  <div key={k} data-testid={`perm-${k}`} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                    <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap" }}>
                      <Checkbox
                        checked={!!value[k]}
                        disabled={readonly || !!disabledReason}
                        onChange={(e) => toggle(k, e.target.checked)}
                      >
                        <b style={{ fontSize: 13 }}>{m?.label || meta.labels[k] || k}</b>
                      </Checkbox>
                      <Tag color={sens.color} style={{ marginInlineStart: 0 }}>{sens.text}</Tag>
                      {sourceTag(k)}
                      {compare && !!compare.perms[k] !== !!value[k] && (
                        <Tag color="purple">{compare.label}：{compare.perms[k] ? "开" : "关"}</Tag>
                      )}
                      <Popover content={detail(k)} title={m?.label || k} trigger={["hover", "click"]}>
                        <InfoCircleOutlined style={{ color: "#999", marginInlineStart: 4 }} />
                      </Popover>
                    </div>
                    <div style={{ fontSize: 12, color: "#888", paddingInlineStart: 24, lineHeight: 1.5 }}>
                      {m?.summary}
                      {disabledReason && <div style={{ color: "#d46b08" }}>为什么不可用：{disabledReason}</div>}
                    </div>
                  </div>
                );
                return row;
              })}
            </div>
          </section>
        );
      })}

      {meta.groups.every((g) => g.keys.filter(matches).length === 0) && (
        <Empty description="没有匹配的权限项" />
      )}
    </div>
  );
}
