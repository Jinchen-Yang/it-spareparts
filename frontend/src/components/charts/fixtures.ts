import type { BusinessTrendPoint } from "./BusinessTrendChart";
import type { MetricBarItem } from "./HorizontalMetricBar";

/**
 * 图表组件固定演示数据：种子伪随机（mulberry32），任何机器任何时刻生成的
 * 数值逐位一致——demo 截图可复现、测试可精确断言。
 * 后续 BossBoardPage 接真数据时，本文件只供 chart-demo 与测试引用，不进生产包。
 */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const pad = (n: number) => String(n).padStart(2, "0");

/** 120 天日粒度趋势：周内节律 + 三段缺数据（null 断点）+ 若干负毛利日 + 同环比。 */
export function trendDailyFixture(): BusinessTrendPoint[] {
  const rand = mulberry32(20260714);
  const points: BusinessTrendPoint[] = [];
  const start = new Date(2026, 2, 1); // 2026-03-01
  for (let i = 0; i < 120; i++) {
    const d = new Date(start.getTime() + i * 86400000);
    const period = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    // 三段人为断档：验证 null 画成断点而非 0
    if ((i >= 22 && i <= 24) || i === 67 || i === 100) {
      points.push({ period, sales_ex_tax: null, purchase_ex_tax: null, gross_profit: null });
      continue;
    }
    const weekday = d.getDay();
    const weekFactor = weekday === 0 || weekday === 6 ? 0.35 : 1;
    const sales = Math.round((42000 + 30000 * rand() + i * 120) * weekFactor);
    const purchase = Math.round((30000 + 26000 * rand() + i * 90) * weekFactor);
    // 约 1/8 的天亏损（大单亏价出货）：验证负毛利变色 + 0 轴参考线
    const lossDay = rand() < 0.125;
    const profit = lossDay
      ? -Math.round(2000 + 9000 * rand())
      : Math.round(sales * (0.08 + 0.1 * rand()));
    points.push({
      period,
      sales_ex_tax: sales,
      purchase_ex_tax: purchase,
      gross_profit: profit,
      compare: {
        sales_ex_tax: { yoy: 0.4 * rand() - 0.1, mom: 0.2 * rand() - 0.1 },
        gross_profit: { yoy: 0.6 * rand() - 0.25, mom: 0.3 * rand() - 0.15 },
      },
    });
  }
  return points;
}

/** 周粒度：由日数据聚合（ISO 周标签沿用"YYYY-Www"形态）。 */
export function trendWeeklyFixture(): BusinessTrendPoint[] {
  const daily = trendDailyFixture();
  const weeks = new Map<string, BusinessTrendPoint[]>();
  daily.forEach((p, i) => {
    const key = `2026-W${pad(Math.floor(i / 7) + 9)}`;
    if (!weeks.has(key)) weeks.set(key, []);
    weeks.get(key)!.push(p);
  });
  return [...weeks.entries()].map(([period, pts]) => {
    const sum = (k: "sales_ex_tax" | "purchase_ex_tax" | "gross_profit") => {
      const vals = pts.map((p) => p[k]).filter((v): v is number => v != null);
      return vals.length ? vals.reduce((a, b) => a + b, 0) : null;
    };
    return {
      period,
      sales_ex_tax: sum("sales_ex_tax"),
      purchase_ex_tax: sum("purchase_ex_tax"),
      gross_profit: sum("gross_profit"),
    };
  });
}

/** 月粒度。 */
export function trendMonthlyFixture(): BusinessTrendPoint[] {
  const daily = trendDailyFixture();
  const months = new Map<string, BusinessTrendPoint[]>();
  daily.forEach((p) => {
    const key = p.period.slice(0, 7);
    if (!months.has(key)) months.set(key, []);
    months.get(key)!.push(p);
  });
  return [...months.entries()].map(([period, pts]) => {
    const sum = (k: "sales_ex_tax" | "purchase_ex_tax" | "gross_profit") => {
      const vals = pts.map((p) => p[k]).filter((v): v is number => v != null);
      return vals.length ? vals.reduce((a, b) => a + b, 0) : null;
    };
    return {
      period,
      sales_ex_tax: sum("sales_ex_tax"),
      purchase_ex_tax: sum("purchase_ex_tax"),
      gross_profit: sum("gross_profit"),
      compare: { sales_ex_tax: { yoy: 0.18, mom: 0.05 }, gross_profit: { yoy: -0.07, mom: 0.02 } },
    };
  });
}

const PN_POOL = [
  ["00Y2684", "IBM 600GB 10K 6Gbps SAS 2.5英寸 G2HS 硬盘"],
  ["02311JRE", "华为 SAS 硬盘 1.2TB 10K 2.5英寸（含托架）"],
  ["875511-B21", "HPE 960GB SATA 6G Read Intensive SFF SSD"],
  ["7SD7A05707", "联想 ThinkSystem 2.4TB 10K SAS 12Gb 热插拔硬盘"],
  ["UCS-HD24TB10K4KN", "思科 UCS 2.4TB 10K SAS 12G 4Kn 企业级硬盘模组（超长编号验证截断）"],
  ["MZ7LH960HAJR-00005", "三星 PM883 960GB SATA 企业级 SSD"],
  ["ST2400MM0129", "希捷 Exos 2.4TB 10K SAS 12Gb/s 256MB 缓存"],
  ["06200282", "华为 RH2288 V3 电源模块 750W 白金"],
  ["03022XYT", "华为 CE6851 交换机业务板"],
  ["00WG700", "IBM/联想 600GB 15K 12Gbps SAS 2.5英寸 硬盘"],
  ["P04560-B21", "HPE 480GB SATA RI SFF SC DS SSD"],
  ["4XB7A14099", "联想 ThinkSystem DE 系列 1.8TB 10K 硬盘"],
  ["02312GPD", "华为 1200W 交流电源模块"],
  ["N9K-C93180YC-EX", "思科 Nexus 9300 48口 25G 交换机（整机备件）"],
  ["867261-B21", "HPE 阵列卡超级电容组件"],
  ["01DE355", "联想存储 V3700 V2 控制器电池"],
  ["AB572A", "HP Integrity 服务器风扇组件"],
  ["540-7156", "Sun/Oracle 146GB 10K SAS 硬盘（停产老货）"],
] as const;

/** 采购 · 平均单价：18 项含 3 项 null（无采购记录）、1 项超长 PN、部分带池均值/约束价。 */
export function metricPurchaseAvgFixture(): MetricBarItem[] {
  const rand = mulberry32(88);
  return PN_POOL.map(([pn, description], i) => {
    const noData = i === 5 || i === 11 || i === 16;
    const base = 300 + 5200 * rand();
    return {
      part_id: 1000 + i,
      pn,
      description,
      qty: noData ? null : Math.round(2 + 60 * rand()),
      order_count: noData ? null : Math.round(1 + 9 * rand()),
      last_date: noData ? null : `2026-0${1 + Math.floor(6 * rand())}-${pad(1 + Math.floor(27 * rand()))}`,
      value: noData ? null : Math.round(base * 100) / 100,
      pool_avg: i % 3 === 0 ? Math.round(base * (0.85 + 0.2 * rand()) * 100) / 100 : null,
      constraint_price: i % 4 === 0 ? Math.round(base * 0.92 * 100) / 100 : null,
    };
  });
}

/** 销售 · 金额合计：14 项含 2 项 null（窗口内无销售）。 */
export function metricSalesTotalFixture(): MetricBarItem[] {
  const rand = mulberry32(99);
  return PN_POOL.slice(0, 14).map(([pn, description], i) => {
    const noData = i === 3 || i === 9;
    return {
      part_id: 2000 + i,
      pn,
      description,
      qty: noData ? null : Math.round(5 + 120 * rand()),
      order_count: noData ? null : Math.round(1 + 14 * rand()),
      last_date: noData ? null : `2026-0${4 + Math.floor(3 * rand())}-${pad(1 + Math.floor(27 * rand()))}`,
      value: noData ? null : Math.round(20000 + 640000 * rand()),
      pool_avg: i % 2 === 0 ? Math.round(150000 + 220000 * rand()) : null,
      constraint_price: i === 0 ? 4200 : null,
    };
  });
}
