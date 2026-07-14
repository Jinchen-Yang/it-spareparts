/** EChartContainer 生命周期测试：init/dispose、监听不残留、option 引用门控、
 * 事件桥（绑定一次 + 回调取最新 + zr: 前缀）、三态覆盖层。echarts 全程 mock，不碰 canvas。 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

vi.mock("../echartsCore", async () => (await import("./echartsCoreMock")).mockModule);

import { init, lastChart, resetEchartsMock } from "./echartsCoreMock";
import EChartContainer from "../EChartContainer";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  resetEchartsMock();
});

class FakeRO {
  static all: FakeRO[] = [];
  observe = vi.fn();
  disconnect = vi.fn();
  unobserve = vi.fn();
  cb: () => void;
  constructor(cb: () => void) {
    this.cb = cb;
    FakeRO.all.push(this);
  }
}

describe("EChartContainer", () => {
  it("挂载只 init 一次并 setOption（notMerge 防脏系列）", () => {
    const option = { series: [] };
    render(<EChartContainer option={option} />);
    expect(init).toHaveBeenCalledTimes(1);
    const chart = lastChart();
    expect(chart.setOption).toHaveBeenCalledTimes(1);
    expect(chart.setOption).toHaveBeenCalledWith(option, { notMerge: true, lazyUpdate: true });
  });

  it("option 引用不变的重渲染不触发 setOption；引用变了才重画（大数据量不卡顿的约定）", () => {
    const option = { series: [] };
    const { rerender } = render(<EChartContainer option={option} loading={false} />);
    const chart = lastChart();
    rerender(<EChartContainer option={option} loading={true} />);
    rerender(<EChartContainer option={option} loading={false} />);
    expect(chart.setOption).toHaveBeenCalledTimes(1);
    rerender(<EChartContainer option={{ series: [] }} loading={false} />);
    expect(chart.setOption).toHaveBeenCalledTimes(2);
  });

  it("ResizeObserver：尺寸变化经 rAF 合帧后 chart.resize（连触发只跑一次），卸载后 disconnect + dispose", () => {
    FakeRO.all.length = 0;
    vi.stubGlobal("ResizeObserver", FakeRO);
    // rAF 存包不立刻执行：验证"同帧多次 RO 回调只排一次 resize"
    const rafQueue: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => rafQueue.push(cb));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    const { unmount } = render(<EChartContainer option={{}} />);
    const chart = lastChart();
    const ro = FakeRO.all[0];
    expect(ro.observe).toHaveBeenCalledTimes(1);
    ro.cb();
    ro.cb();
    expect(chart.resize).not.toHaveBeenCalled(); // 推迟到下一帧（避开 echarts 主流程）
    expect(rafQueue).toHaveLength(1);
    rafQueue.forEach((cb) => cb(0));
    expect(chart.resize).toHaveBeenCalledTimes(1);
    unmount();
    expect(ro.disconnect).toHaveBeenCalledTimes(1);
    expect(chart.dispose).toHaveBeenCalledTimes(1);
  });

  it("无 ResizeObserver 环境退回 window resize，卸载时用同一引用摘除（无残留监听）", () => {
    // src/test/setup.ts 为 antd 垫了全局 ResizeObserver，这里显式抹掉才能走兜底分支
    vi.stubGlobal("ResizeObserver", undefined);
    const added: [string, EventListener][] = [];
    const addSpy = vi.spyOn(window, "addEventListener").mockImplementation((t, h) => {
      added.push([t as string, h as EventListener]);
    });
    const removeSpy = vi.spyOn(window, "removeEventListener");
    const { unmount } = render(<EChartContainer option={{}} />);
    const resizeAdds = added.filter(([t]) => t === "resize");
    expect(resizeAdds).toHaveLength(1);
    unmount();
    expect(removeSpy).toHaveBeenCalledWith("resize", resizeAdds[0][1]);
    expect(lastChart().dispose).toHaveBeenCalledTimes(1);
    addSpy.mockRestore();
    removeSpy.mockRestore();
  });

  it("事件桥：只绑一次，回调换新后仍打到最新函数；卸载时 off 干净", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender, unmount } = render(
      <EChartContainer option={{}} onEvents={{ click: first }} />,
    );
    const chart = lastChart();
    expect(chart.on).toHaveBeenCalledTimes(1);
    chart.emit("click", { a: 1 });
    expect(first).toHaveBeenCalledWith({ a: 1 }, chart); // 第二参=chart 实例（像素反解用）
    rerender(<EChartContainer option={{}} onEvents={{ click: second }} />);
    expect(chart.on).toHaveBeenCalledTimes(1); // 不重绑
    chart.emit("click", { a: 2 });
    expect(second).toHaveBeenCalledWith({ a: 2 }, chart);
    expect(first).toHaveBeenCalledTimes(1);
    unmount();
    expect(chart.off).toHaveBeenCalledTimes(1);
    expect(chart.handlers.click ?? []).toHaveLength(0);
  });

  it("zr: 前缀事件绑到 zrender 层，卸载时同样摘干净", () => {
    const onZrClick = vi.fn();
    const { unmount } = render(
      <EChartContainer option={{}} onEvents={{ "zr:click": onZrClick }} />,
    );
    const chart = lastChart();
    expect(chart.on).not.toHaveBeenCalled(); // 不落在 chart 事件上
    expect(chart.zr.on).toHaveBeenCalledTimes(1);
    chart.zr.emit("click", { offsetX: 3, offsetY: 4 });
    expect(onZrClick).toHaveBeenCalledWith({ offsetX: 3, offsetY: 4 }, chart);
    unmount();
    expect(chart.zr.handlers.click ?? []).toHaveLength(0);
  });

  it("loading / error / empty 三态覆盖层；error 优先于 empty", () => {
    const { rerender } = render(<EChartContainer option={{}} loading />);
    expect(screen.getByTestId("chart-loading")).toBeTruthy();
    rerender(<EChartContainer option={{}} error="500" empty />);
    expect(screen.getByTestId("chart-error").textContent).toContain("500");
    expect(screen.queryByTestId("chart-empty")).toBeNull();
    rerender(<EChartContainer option={{}} empty emptyText="窗口内无数据" />);
    expect(screen.getByTestId("chart-empty").textContent).toBe("窗口内无数据");
  });

  it("图表容器宽度 100%（小屏不把页面撑出横向滚动）", () => {
    render(<EChartContainer option={{}} testId="wrap" height={300} />);
    const wrap = screen.getByTestId("wrap");
    expect(wrap.style.width).toBe("100%");
    const canvasHost = wrap.firstElementChild as HTMLElement;
    expect(canvasHost.style.width).toBe("100%");
    expect(canvasHost.style.height).toBe("300px");
  });
});
