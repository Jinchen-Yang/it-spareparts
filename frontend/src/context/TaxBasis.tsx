// 全局「含税 / 不含税」显示口径开关（合同重点）。
// 口径只影响价格「怎么展示」，不做任何税率换算——含税/不含税两套都来自 Excel 原值，
// 缺的一侧显示 "—"（详见后端 purchase_analysis._line_prices 与采购记录列渲染）。
import { createContext, useContext, useState, type ReactNode } from "react";
import { Segmented } from "antd";
import { money } from "../utils/format";

export type TaxBasis = "inc" | "ex" | "both";

export const TAX_BASIS_OPTIONS = [
  { label: "含税", value: "inc" },
  { label: "不含税", value: "ex" },
  { label: "两列", value: "both" },
];

const KEY = "tax_basis";

function initial(): TaxBasis {
  const v = localStorage.getItem(KEY);
  return v === "inc" || v === "ex" || v === "both" ? v : "both";
}

interface Ctx {
  basis: TaxBasis;
  setBasis: (b: TaxBasis) => void;
}

const TaxBasisContext = createContext<Ctx>({ basis: "both", setBasis: () => {} });

export function TaxBasisProvider({ children }: { children: ReactNode }) {
  const [basis, setBasisState] = useState<TaxBasis>(initial);
  const setBasis = (b: TaxBasis) => {
    localStorage.setItem(KEY, b);
    setBasisState(b);
  };
  return (
    <TaxBasisContext.Provider value={{ basis, setBasis }}>
      {children}
    </TaxBasisContext.Provider>
  );
}

export const useTaxBasis = () => useContext(TaxBasisContext);

// 内联双值（卡片/Statistic 用；表格请改用两列）。跟随全局开关：
// inc→只含税、ex→只不含税、both→「含 X / 不含 Y」。缺的一侧由 money() 显示 "-"。
export function TaxMoney({ inc, ex }: { inc: number | null; ex: number | null }) {
  const { basis } = useTaxBasis();
  if (basis === "inc") return <>{money(inc)}</>;
  if (basis === "ex") return <>{money(ex)}</>;
  return (
    <span style={{ whiteSpace: "nowrap" }}>
      <span style={{ color: "var(--mb-text-3)" }}>含 </span>{money(inc)}
      <span style={{ color: "var(--mb-text-3)" }}> · 不含 </span>{money(ex)}
    </span>
  );
}

/** 顶栏全局口径开关：含税 / 不含税 / 两列都显示。全站价格列跟随。 */
export function TaxBasisToggle() {
  const { basis, setBasis } = useTaxBasis();
  return (
    <Segmented
      size="small"
      options={TAX_BASIS_OPTIONS}
      value={basis}
      onChange={(v) => setBasis(v as TaxBasis)}
    />
  );
}
