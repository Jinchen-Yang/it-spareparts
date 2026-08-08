import { Space, Tag } from "antd";

import type { MaintenanceContractSummary } from "../../api/maintenanceOperations";
import { money } from "../../utils/format";

function contractTags(contract: MaintenanceContractSummary) {
  const tags = [];
  if (contract.is_effective && contract.included_in_total) {
    tags.push(<Tag color="green" key="included">计入合同总额</Tag>);
  } else if (!contract.is_effective) {
    tags.push(<Tag key="historical">非当前有效</Tag>);
  } else {
    tags.push(<Tag key="excluded">不计入合同总额</Tag>);
  }
  if (contract.status_mapping_state !== "mapped") {
    tags.push(<Tag color="orange" key="unmapped">状态未映射</Tag>);
  }
  if (contract.amount_status === "missing") {
    tags.push(<Tag color="orange" key="missing">金额缺失</Tag>);
  } else if (contract.amount_status === "restricted") {
    tags.push(<Tag key="restricted">金额不可见</Tag>);
  }
  return tags;
}

export default function ContractPortfolio({
  contracts,
  compact = false,
}: {
  contracts: MaintenanceContractSummary[];
  compact?: boolean;
}) {
  if (contracts.length === 0) {
    return <div style={{ color: "var(--mb-text-3)", fontSize: 12.5 }}>尚未关联有效合同</div>;
  }
  return (
    <div aria-label="项目合同清单" style={{ display: "grid", gap: compact ? 7 : 10 }}>
      {contracts.map((contract) => (
        <div
          key={contract.project_contract_id}
          style={{
            border: "1px solid var(--mb-border-soft)",
            borderRadius: 8,
            background: "var(--mb-inset)",
            padding: compact ? "7px 9px" : "10px 12px",
            minWidth: 0,
          }}
        >
          <div style={{
            display: "flex", justifyContent: "space-between", gap: 10,
            alignItems: "baseline", flexWrap: "wrap",
          }}>
            <strong style={{ overflowWrap: "anywhere" }}>{contract.contract_no}</strong>
            <span style={{ fontVariantNumeric: "tabular-nums" }}>
              {contract.amount_status === "restricted" ? "—" : money(contract.contract_amount)}
            </span>
          </div>
          <Space wrap size={[4, 4]} style={{ marginTop: 5 }}>
            {contractTags(contract)}
          </Space>
        </div>
      ))}
    </div>
  );
}
