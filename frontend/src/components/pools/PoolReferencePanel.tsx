import { useAuthorizedQuery, useModuleAccess, AuthorizedQueryError, useRouteAccess } from "../../authz/access";
import { actionRule } from "../../authz/rules";
import { Card } from "antd";
import { fetchPoolReference } from "../../api/poolAnalysis";
import type { PoolAnalysisSide } from "../../api/poolAnalysis";
import type { PoolAnalysisRange } from "../../api/poolAnalysis";
import PoolReferenceCard from "./PoolReferenceCard";
import { useLocalRestrictions } from "../../pages/boss/shared";
import { useTaxDisplayPolicy, TaxPolicyBoundary } from "../../context/TaxBasis";

export default function PoolReferencePanel({
  partId,
  side = "both",
  range = "90d",
  dateFrom,
  dateTo,
  purchaseType,
  compact = false,
  summary = false,
}: {
  partId: number;
  side?: "both" | PoolAnalysisSide;
  range?: PoolAnalysisRange;
  dateFrom?: string;
  dateTo?: string;
  purchaseType?: string;
  compact?: boolean;
  summary?: boolean;
}) {
  const canOpenDetail = useRouteAccess("/pool-analysis/:groupId").allowed;
  const local = useLocalRestrictions("pool_price_analysis");
  const access = useModuleAccess("pool_price_analysis");
  const visible = access.surface("part_reference", true);
  const tax = useTaxDisplayPolicy(), taxReady = !tax.loading && !tax.loadFailed;
  const request = useAuthorizedQuery({rule: actionRule("pool_price_analysis", "read"),
    legacy: {pool_price_analysis: true}, queryKey: ["pool-reference", partId, range, dateFrom, dateTo, purchaseType],
    enabled: visible && taxReady && partId > 0,
    query: context => fetchPoolReference(partId, {range, date_from: dateFrom, date_to: dateTo, purchase_type: purchaseType}, context)});
  const reference = request.data, loading = request.loading;
  if (!visible) return null;
  if (!taxReady) return <TaxPolicyBoundary><></></TaxPolicyBoundary>;
  if (loading) return summary ? <span>互通池加载中…</span> : <Card size="small" loading aria-label="池价格参考加载中" />;
  if (request.error) return <AuthorizedQueryError error={request.error} retry={request.retry} />;
  if (!reference) return null;
  return <PoolReferenceCard reference={reference} side={side} compact={compact} summary={summary}
    forceRestricted={local.governance} canOpenDetail={canOpenDetail} />;
}
