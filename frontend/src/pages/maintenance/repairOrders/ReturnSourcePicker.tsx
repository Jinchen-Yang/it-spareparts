import { useEffect, useState } from "react";
import { Alert, Button, Empty, Input, Pagination, Spin } from "antd";
import { listRepairReturnCandidates, type RepairReturnCandidate } from "../../../api/inspectionRepair";
import { parseApiError } from "../../../utils/apiError";
import { formatBusinessDateTime } from "../../../utils/date";

const pageSize = 6;

interface SelectedSource {
  sourceVersion?: number | null;
  sourceAvailableQty?: number;
}

export default function ReturnSourcePicker({
  onPick, selected, refresh, busy,
}: {
  onPick: (candidate: RepairReturnCandidate) => void;
  selected: ReadonlyMap<string, SelectedSource>;
  refresh: number;
  busy: boolean;
}) {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [reload, setReload] = useState(0);
  const requestKey = JSON.stringify([query, page, reload, refresh]);
  const [response, setResponse] = useState<{
    key: string;
    items: RepairReturnCandidate[];
    total: number;
    error?: string;
  }>();
  const current = response?.key === requestKey ? response : undefined;

  useEffect(() => {
    const controller = new AbortController();
    listRepairReturnCandidates(query, page, pageSize, controller.signal)
      .then(({ items, total }) => {
        if (controller.signal.aborted) return;
        const lastPage = Math.max(1, Math.ceil(total / pageSize));
        if (page > lastPage) {
          setPage(lastPage);
          return;
        }
        setResponse({ key: requestKey, items, total });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setResponse({ key: requestKey, items: [], total: 0, error: parseApiError(error).message });
        }
      });
    return () => controller.abort();
  }, [query, page, requestKey]);

  return (
    <section className="wo-return-picker" aria-label="维保备件返还">
      <h3>维保备件返还</h3>
      <p>仅显示可送修且有剩余数量的坏件。</p>
      <Input.Search
        aria-label="搜索返还编号或备件 PN"
        placeholder="搜索返还编号、PN 或描述"
        allowClear
        enterButton="搜索"
        maxLength={120}
        onSearch={(value) => { setQuery(value.trim()); setPage(1); }}
      />
      {!current && <div className="wo-return-loading" role="status"><Spin size="small" /> 正在加载返还明细</div>}
      {current?.error && <Alert showIcon type="error" message={current.error}
        action={<Button size="small" onClick={() => setReload(value => value + 1)}>重试</Button>} />}
      {current && !current.error && current.items.length === 0 && <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE} description={query ? "没有匹配的可送修返还明细" : "暂无可送修返还明细"} />}
      {current && current.items.length > 0 && <>
        <ul className="wo-return-list">
          {current.items.map(candidate => {
            const existing = selected.get(candidate.receiptId);
            const unchanged = existing && existing.sourceVersion === candidate.version
              && existing.sourceAvailableQty === candidate.availableQty;
            return (
              <li key={candidate.receiptId} className="wo-return-row">
                <div>
                  <strong>{candidate.pn}</strong>
                  {candidate.description && <p>{candidate.description}</p>}
                  <small>{candidate.headNo || "未登记来源单号"} · {formatBusinessDateTime(candidate.occurredAt)}</small>
                  <span className="wo-return-count">{candidate.condition} · 可选 {candidate.availableQty} 件</span>
                </div>
                <Button size="small" disabled={busy || !!unchanged} onClick={() => onPick(candidate)}
                  aria-label={`${existing ? "更新" : "添加"}返还 ${candidate.headNo || ""} ${candidate.pn}`}>
                  {unchanged ? "已加入" : existing ? "更新来源" : "加入维修单"}
                </Button>
              </li>
            );
          })}
        </ul>
        <Pagination size="small" current={page} pageSize={pageSize} total={current.total}
          hideOnSinglePage showSizeChanger={false} onChange={setPage} />
      </>}
    </section>
  );
}
