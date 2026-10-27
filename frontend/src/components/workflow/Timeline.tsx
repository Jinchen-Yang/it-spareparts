import { useEffect, useRef, useState, type ReactNode } from "react";
import { Alert, Button, Empty, Space, Spin, Timeline as AntTimeline, Typography } from "antd";
import { listSubjectEvents, type SubjectEvent } from "../../api/workItems";
import { formatBusinessDateTime } from "../../utils/date";

interface Props {
  subjectType: string;
  subjectId: number;
  renderEvent?: (event: SubjectEvent) => ReactNode;
}

const kindLabels: Record<string, string> = {
  open: "创建了待办", claim: "领取了待办", start: "开始处理待办", release: "退回了待办",
  transfer: "转交了待办", reassign: "改派了待办", complete: "完成了待办", decline: "拒绝了待办",
  cancel: "取消了待办", cancel_subject: "取消了待办", pause: "暂停了待办", pause_subject: "暂停了待办",
  resume: "恢复了待办", resume_subject: "恢复了待办",
};

function defaultEvent(event: SubjectEvent) {
  const actor = event.actor_name || (event.actor === null ? "系统" : `用户 ${event.actor}`);
  const reason = typeof event.payload.reason === "string" ? event.payload.reason : "";
  return <><div>{actor} {kindLabels[event.kind] || event.kind}</div>{reason && <Typography.Text type="secondary">原因：{reason}</Typography.Text>}</>;
}

export default function Timeline({ subjectType, subjectId, renderEvent }: Props) {
  const [items, setItems] = useState<SubjectEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  const controller = useRef<AbortController>();
  const sequence = useRef(0);
  const pageRef = useRef(1);

  const load = async (nextPage: number, reset = false) => {
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    const requestSequence = ++sequence.current;
    setLoading(true);
    setError(undefined);
    try {
      const data = await listSubjectEvents(subjectType, subjectId, { page: nextPage, page_size: 20 }, request.signal);
      if (request.signal.aborted || requestSequence !== sequence.current) return;
      setItems((current) => {
        const unique = new Map((reset ? [] : current).map((event) => [event.id, event]));
        data.items.forEach((event) => unique.set(event.id, event));
        return [...unique.values()].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at) || b.id - a.id);
      });
      setTotal(data.total);
      setPage(nextPage);
      pageRef.current = nextPage;
    } catch (error: unknown) {
      if (request.signal.aborted || requestSequence !== sequence.current) return;
      const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      setError(typeof detail === "string" ? detail : "时间线加载失败，请重试");
    } finally {
      if (!request.signal.aborted && requestSequence === sequence.current) setLoading(false);
    }
  };

  useEffect(() => {
    setItems([]);
    setTotal(0);
    setPage(1);
    pageRef.current = 1;
    void load(1, true);
    return () => { controller.current?.abort(); ++sequence.current; };
  }, [subjectType, subjectId]);

  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      {error && <Alert type="error" showIcon message={error} action={<Button onClick={() => void load(items.length ? pageRef.current + 1 : 1, !items.length)}>重试</Button>} />}
      {!!items.length && <AntTimeline items={items.map((event) => ({ key: event.id, children: <><time dateTime={event.created_at}>{formatBusinessDateTime(event.created_at)}</time>{renderEvent ? renderEvent(event) : defaultEvent(event)}</> }))} />}
      {loading && <Spin aria-label="时间线加载中" />}
      {!loading && !error && !items.length && <Empty description="暂无记录" />}
      {items.length < total && <Button loading={loading} disabled={loading} onClick={() => void load(page + 1)}>加载更多</Button>}
    </Space>
  );
}
