import { useEffect, useRef, useState } from "react";
import { Alert, Card } from "antd";
import { fetchPoolReference } from "../../api/poolAnalysis";
import type { PoolAnalysisSide, PoolReference } from "../../api/poolAnalysis";
import type { PoolAnalysisRange } from "../../api/poolAnalysis";
import PoolReferenceCard from "./PoolReferenceCard";
import { useLocalRestrictions } from "../../pages/boss/shared";

export default function PoolReferencePanel({
  partId,
  side = "both",
  range = "90d",
  dateFrom,
  dateTo,
  purchaseType,
  compact = false,
}: {
  partId: number;
  side?: "both" | PoolAnalysisSide;
  range?: PoolAnalysisRange;
  dateFrom?: string;
  dateTo?: string;
  purchaseType?: string;
  compact?: boolean;
}) {
  const local = useLocalRestrictions();
  const [reference, setReference] = useState<PoolReference | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const seq = useRef(0);

  useEffect(() => {
    const request = ++seq.current;
    setReference(null);
    setFailed(false);
    setLoading(true);
    fetchPoolReference(partId, {
      range,
      date_from: dateFrom,
      date_to: dateTo,
      purchase_type: purchaseType,
    })
      .then((data) => { if (request === seq.current) setReference(data); })
      .catch(() => { if (request === seq.current) setFailed(true); })
      .finally(() => { if (request === seq.current) setLoading(false); });
  }, [partId, range, dateFrom, dateTo, purchaseType]);

  if (loading) return <Card size="small" loading aria-label="池价格参考加载中" />;
  if (failed) return <Alert type="warning" showIcon message="池价格参考加载失败，请稍后重试" />;
  if (!reference) return null;
  return <PoolReferenceCard reference={reference} side={side} compact={compact}
    forceRestricted={local.governance} />;
}
