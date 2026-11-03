import { useEffect, useRef, useState } from "react";
import { Alert, Button, Drawer, Input, Space, Table, Tag } from "antd";
import { Link } from "react-router-dom";
import { listMaintenanceProjects, searchMaintenanceProjects, type MaintenanceProject } from "../../api/maintenanceProjects";
import { useSessionOwner, sessionStale } from "../../utils/session";

export default function MaintenanceProjectDirectoryButton() {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [draft, setDraft] = useState("");
  const [page, setPage] = useState(1);
  const [rows, setRows] = useState<MaintenanceProject[]>([]);
  const [total, setTotal] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const seq = useRef(0);
  const owner = useSessionOwner();
  useEffect(() => {
    const key = ++seq.current;
    if (!open) return;
    if (sessionStale(owner)) { setRows([]); setTotal(0); setError("登录账号已变化，请刷新后继续"); return; }
    setBusy(true); setRows([]); setError(null);
    const params = { page, page_size: 20, include_inactive: true };
    (q ? searchMaintenanceProjects({ ...params, q }) : listMaintenanceProjects(params))
      .then(({ data }) => { if (key === seq.current && !sessionStale(owner)) { setRows(data.rows); setTotal(data.total); } })
      .catch(() => { if (key === seq.current && !sessionStale(owner)) setError("项目目录加载失败，请重试"); })
      .finally(() => { if (key === seq.current) setBusy(false); });
    return () => { seq.current++; };
  }, [open, q, page, retry, owner]);
  return <>
    <Button onClick={() => setOpen(true)}>项目目录（含归档）</Button>
    <Drawer title="项目目录（含归档）" open={open} onClose={() => setOpen(false)} width={760}>
      <p>归档项目仍可在此找到。进入项目后，有管理权限的账号可恢复；归档不删除历史事实。</p>
      <Space direction="vertical" style={{ width: "100%" }}>
        <Input.Search aria-label="搜索含归档项目" value={draft} onChange={event => setDraft(event.target.value)}
          onSearch={value => { setQ(value.trim()); setPage(1); }} placeholder="项目名称或编号" />
        {error && <Alert type="error" message={error} action={<Button onClick={() => setRetry(value => value + 1)}>重试</Button>} />}
        <Table<MaintenanceProject> rowKey="project_id" size="small" loading={busy} dataSource={rows}
          columns={[
            { title: "项目", render: (_, row) => <Link to={`/maintenance/projects/${encodeURIComponent(row.project_id)}`} onClick={() => setOpen(false)}>{row.display_name}</Link> },
            { title: "项目编号", dataIndex: "project_code" },
            { title: "主档状态", render: (_, row) => <Tag>{row.is_active ? "有效" : "已归档"}</Tag> },
          ]} pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: setPage }} />
      </Space>
    </Drawer>
  </>;
}
