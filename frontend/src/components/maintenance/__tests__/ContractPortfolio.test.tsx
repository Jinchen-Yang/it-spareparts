import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import ContractPortfolio from "../ContractPortfolio";

afterEach(cleanup);

describe("ContractPortfolio", () => {
  it("优先说明合同不计入总额，不把已排除合同误标为仅非当前有效", () => {
    render(<ContractPortfolio contracts={[{
      project_contract_id: "pc-excluded",
      contract_id: "contract-excluded",
      contract_no: "XS-EXCLUDED",
      contract_amount: 1000,
      contract_amount_basis: "inc_tax",
      contract_status: "已结束",
      status_mapping_state: "mapped",
      included_in_total: false,
      is_effective: false,
      amount_status: "available",
      received_amount: null,
    }]} />);

    const portfolio = screen.getByLabelText("项目合同清单");
    expect(within(portfolio).getByText("合同额（含税）")).toBeInTheDocument();
    expect(within(portfolio).getByText("¥1,000")).toBeInTheDocument();
    expect(within(portfolio).getByText("不计入合同总额")).toBeInTheDocument();
    expect(within(portfolio).queryByText("非当前有效")).toBeNull();
  });

  it("合同额税口径缺失时不把金额冒充为含税", () => {
    render(<ContractPortfolio contracts={[{
      project_contract_id: "pc-unknown",
      contract_id: "contract-unknown",
      contract_no: "XS-UNKNOWN",
      contract_amount: 1000,
      contract_amount_basis: null,
      contract_status: "已生效",
      status_mapping_state: "mapped",
      included_in_total: true,
      is_effective: true,
      amount_status: "available",
      received_amount: null,
    }]} />);

    const portfolio = screen.getByLabelText("项目合同清单");
    expect(within(portfolio).getByText("合同额税口径不可确认")).toBeInTheDocument();
    expect(within(portfolio).queryByText("¥1,000")).toBeNull();
  });
});
