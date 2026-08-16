import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Alert, Card, Input, Select, Space, Typography } from "antd";
import {
  getBoardProjects,
  searchBoardProjects,
  type BoardProjectRow,
} from "../../../api/maintenanceBossBoard";
import BossProjectTable from "../../../components/maintenance/boss/BossProjectTable";

const { Title, Text } = Typography;

const LIFECYCLE_OPTIONS = [
  { value: "all", label: "全部生命周期" },
  { value: "ongoing", label: "进行中" },
  { value: "ended", label: "已结束" },
  { value: "missing", label: "期限缺失" },
];

/**
 * 全项目分页列表（plan v1.3 §5.1）。服务端分页/筛选/排序；筛选状态存 URL。
 * 自由文本搜索走 POST /projects/search（GET 带 q 后端返 422，仓库既有约定）。
 */
export default function BossProjectListPage() {
  const [params, setParams] = useSearchParams();
  const page = Number(params.get("page") || 1);
  const pageSize = Number(params.get("pageSize") || 20);
  const lifecycle = params.get("lifecycle") || "all";
  const q = params.get("q") || "";

  const [rows, setRows] = useState<BoardProjectRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const patch = useCallback(
    (next: Record<string, string | number | undefined>) => {
      const merged = new URLSearchParams(params);
      Object.entries(next).forEach(([key, value]) => {
        if (value === undefined || value === "") merged.delete(key);
        else merged.set(key, String(value));
      });
      setParams(merged, { replace: true });
    },
    [params, setParams],
  );

  useEffect(() => {
    const ticket = ++seq.current;
    setLoading(true);
    const request = q
      ? searchBoardProjects({ q, page, page_size: pageSize, lifecycle })
      : getBoardProjects({ page, page_size: pageSize, lifecycle });
    request
      .then((resp) => {
        if (ticket !== seq.current) return;
        setRows(resp.data.rows);
        setTotal(resp.data.total);
        setError(null);
      })
      .catch(() => {
        if (ticket === seq.current) setError("项目列表加载失败，请稍后重试");
      })
      .finally(() => {
        if (ticket === seq.current) setLoading(false);
      });
  }, [page, pageSize, lifecycle, q]);

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Title level={4} style={{ margin: 0 }}>
        全部维保项目
      </Title>
      <Text type="secondary" style={{ fontSize: 11.5 }}>
        含「未归属」桶：尚未人工确认归属的需求单在此汇总，不静默丢失。
      </Text>
      {error ? <Alert type="error" showIcon message={error} /> : null}
      <Card size="small">
        <Space wrap style={{ marginBottom: 12 }}>
          <Input.Search
            allowClear
            placeholder="搜索项目名称或编码"
            defaultValue={q}
            style={{ width: 260 }}
            onSearch={(value) => patch({ q: value, page: 1 })}
          />
          <Select
            value={lifecycle}
            options={LIFECYCLE_OPTIONS}
            style={{ width: 160 }}
            onChange={(value) => patch({ lifecycle: value, page: 1 })}
          />
        </Space>
        <BossProjectTable
          rows={rows}
          total={total}
          page={page}
          pageSize={pageSize}
          loading={loading}
          onChange={(nextPage, nextSize) =>
            patch({ page: nextPage, pageSize: nextSize })
          }
        />
      </Card>
    </Space>
  );
}
