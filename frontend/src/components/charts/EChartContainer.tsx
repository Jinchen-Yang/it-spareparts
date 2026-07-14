import { useEffect, useRef } from "react";
import { Spin } from "antd";
import { echarts, CHART_THEME_NAME, type ECOption, type EChartsInstance } from "./echartsCore";
import { COLORS } from "../../theme";

/**
 * ECharts 的 React 生命周期壳：init/dispose、容器尺寸自适应、事件桥、
 * loading/空/错误三态。业务组件只负责产出 option（纯函数，可单测），
 * 不允许各自持有 echarts 实例——实例管理的坑（泄漏/重复 init/事件重绑）收敛在这一处。
 */
export interface EChartContainerProps {
  option: ECOption;
  loading?: boolean;
  /** 非空字符串即进入错误态（盖过空态）；页面级重试按钮由调用方放在卡片上。 */
  error?: string | null;
  /** 空态由调用方判定（"数据为空"是业务口径：全 null 也算空，容器不猜）。 */
  empty?: boolean;
  emptyText?: string;
  height?: number;
  /**
   * 事件桥：key 为 echarts 事件名（如 click）；`zr:` 前缀绑到 zrender 层
   * （如 zr:click——整个画布可命中，折线太细点不中时用它 + convertFromPixel 反解）。
   * **键集合以首次渲染为准**（挂载时绑定一次，回调经 ref 转发最新值——
   * 既不随 render 重绑造成泄漏，也不吃闭包陈旧值）。回调第二参为 chart 实例，
   * 供 containPixel/convertFromPixel 等像素反解使用。
   */
  onEvents?: Record<string, (params: unknown, chart: EChartsInstance) => void>;
  /** 无障碍：图表对读屏软件的一句话描述（echarts aria 选项在 option 里另行开启）。 */
  ariaLabel?: string;
  testId?: string;
}

const overlayStyle: React.CSSProperties = {
  position: "absolute",
  inset: 0,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  background: "rgba(255,255,255,.72)",
  zIndex: 2,
  fontSize: 13,
};

export default function EChartContainer({
  option, loading = false, error = null, empty = false,
  emptyText = "窗口内无数据", height = 320, onEvents, ariaLabel, testId,
}: EChartContainerProps) {
  const elRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<EChartsInstance | null>(null);
  const eventsRef = useRef(onEvents);
  eventsRef.current = onEvents;

  // 实例生命周期：挂载 init 一次，卸载 dispose + 摘干净所有监听（RO/兜底 window resize）。
  useEffect(() => {
    const el = elRef.current;
    if (!el) return;
    const chart = echarts.init(el, CHART_THEME_NAME);
    chartRef.current = chart;

    const zr = chart.getZr();
    const bound = Object.keys(eventsRef.current ?? {}).map((key) => {
      const handler = (params: unknown) => eventsRef.current?.[key]?.(params, chart);
      if (key.startsWith("zr:")) {
        const type = key.slice(3);
        zr.on(type, handler);
        return { off: () => zr.off(type, handler) };
      }
      chart.on(key, handler);
      return { off: () => chart.off(key, handler) };
    });

    // resize 必须推迟到下一帧：RO 回调可能落在 echarts 主流程/浏览器布局期内，
    // 同步 resize 会被 echarts 拒绝（"resize should not be called during main process"），
    // canvas 卡在旧宽度、小屏直接横向溢出。rAF 合帧同时天然去抖。
    let pendingResize: number | null = null;
    const hasRaf = typeof requestAnimationFrame === "function";
    const scheduleResize = () => {
      if (pendingResize != null) return;
      const run = () => { pendingResize = null; chart.resize(); };
      pendingResize = hasRaf ? requestAnimationFrame(run) : (setTimeout(run, 0) as unknown as number);
    };
    let ro: ResizeObserver | null = null;
    let winResize: (() => void) | null = null;
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(scheduleResize);
      ro.observe(el);
    } else {
      winResize = scheduleResize;
      window.addEventListener("resize", winResize);
    }

    return () => {
      bound.forEach((b) => b.off());
      ro?.disconnect();
      if (winResize) window.removeEventListener("resize", winResize);
      if (pendingResize != null) {
        if (hasRaf) cancelAnimationFrame(pendingResize);
        else clearTimeout(pendingResize);
      }
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  // option 引用变了才 setOption：调用方用 useMemo 产出 option，无关 state 变化
  // （分页、抽屉开合）不会触发整图重算——这是大数据量不卡顿的关键约定。
  useEffect(() => {
    chartRef.current?.setOption(option, { notMerge: true, lazyUpdate: true });
  }, [option]);

  return (
    <div style={{ position: "relative", width: "100%" }} data-testid={testId}>
      {/* overflow hidden 必须保留：echarts HTML tooltip 隐藏用 visibility:hidden，
        * 布局盒残留在旧坐标；宽屏悬浮过再缩窗（手机旋转）会把页面撑出横向滚动。
        * 配合 option 里 tooltip.confine=true，可见 tooltip 不会被裁。 */}
      <div ref={elRef} role="img" aria-label={ariaLabel} style={{ width: "100%", height, overflow: "hidden" }} />
      {loading && (
        <div style={overlayStyle} data-testid="chart-loading"><Spin /></div>
      )}
      {!loading && error != null && error !== "" && (
        <div style={{ ...overlayStyle, color: COLORS.danger }} data-testid="chart-error">
          加载失败：{error}
        </div>
      )}
      {!loading && (error == null || error === "") && empty && (
        <div style={{ ...overlayStyle, color: COLORS.text3 }} data-testid="chart-empty">
          {emptyText}
        </div>
      )}
    </div>
  );
}
