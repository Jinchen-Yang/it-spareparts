/** Static, text-first bars for a screenshot. Ranking and exact display values belong to the caller. */
export interface PnCompactRankingItem {
  key: string;
  pn: string;
  /** Decimal is converted only for bar geometry; displayed text never passes through Number. */
  value: string | null;
  displayValue: string;
  note?: string;
}

export function MaintenancePnCompactRanking({ items, title, metricLabel, visibleCount = 15, testId }: {
  items: PnCompactRankingItem[];
  title: string;
  metricLabel: string;
  visibleCount?: number;
  testId?: string;
}) {
  const rows = items.slice(0, visibleCount);
  const values = rows.map(row => {
    const value = row.value == null ? 0 : Number(row.value);
    return Number.isFinite(value) ? value : 0;
  });
  const minimum = Math.min(0, ...values);
  const maximum = Math.max(0, ...values);
  const span = maximum - minimum || 1;
  const zero = -minimum / span * 100;

  return <section className="pn-top-bar" role="group" aria-label={title} data-testid={testId}>
    <h3>{title}</h3>
    <p className="pn-top-note">{metricLabel}</p>
    {!rows.length ? <p className="pn-top-empty">当前期间和筛选条件下没有记录</p> : <ol className="pn-top-list">
      {rows.map((row, index) => <li key={row.key} className="pn-top-row">
        <span className="pn-top-index" aria-hidden="true">{index + 1}</span>
        <span className="pn-top-name">{row.pn}</span>
        <span className="pn-top-value">{row.displayValue}{row.note && <small>{row.note}</small>}</span>
        <span className="pn-top-track" aria-hidden="true">
          <i className="pn-top-zero" style={{ left: `${zero}%` }} />
          {row.value != null && values[index] !== 0 && <i className={`pn-top-fill${values[index] < 0 ? " is-negative" : ""}`} style={{
            left: `${values[index] < 0 ? zero + values[index] / span * 100 : zero}%`,
            width: `${Math.abs(values[index]) / span * 100}%`,
          }} />}
        </span>
      </li>)}
    </ol>}
  </section>;
}

export default MaintenancePnCompactRanking;
