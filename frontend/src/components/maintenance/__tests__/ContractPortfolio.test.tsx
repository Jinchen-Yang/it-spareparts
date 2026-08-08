import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import ContractPortfolio from "../ContractPortfolio";

describe("ContractPortfolio", () => {
  it("优先说明合同不计入总额，不把已排除合同误标为仅非当前有效", () => {
    render(<ContractPortfolio contracts={[{
      project_contract_id: "pc-excluded",
      contract_id: "contract-excluded",
      contract_no: "XS-EXCLUDED",
      contract_amount: 1000,
      contract_status: "已结束",
      status_mapping_state: "mapped",
      included_in_total: false,
      is_effective: false,
      amount_status: "available",
      received_amount: null,
    }]} />);

    const portfolio = screen.getByLabelText("项目合同清单");
    expect(within(portfolio).getByText("不计入合同总额")).toBeInTheDocument();
    expect(within(portfolio).queryByText("非当前有效")).toBeNull();
  });
});
