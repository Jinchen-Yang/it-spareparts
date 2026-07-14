/** echartsCore 的测试替身（三个图表测试文件共用）：
 * `vi.mock("../echartsCore", async () => (await import("./echartsCoreMock")).mockModule)`。
 * 不是测试文件，vitest 不会收集。 */
import { vi } from "vitest";

type Handler = (p: unknown) => void;

class FakeZr {
  handlers: Record<string, Handler[]> = {};
  on = vi.fn((t: string, h: Handler) => {
    (this.handlers[t] ??= []).push(h);
  });
  off = vi.fn((t: string, h: Handler) => {
    this.handlers[t] = (this.handlers[t] ?? []).filter((x) => x !== h);
  });
  emit(t: string, params: unknown) {
    (this.handlers[t] ?? []).forEach((h) => h(params));
  }
}

export class FakeChart {
  setOption = vi.fn();
  resize = vi.fn();
  dispose = vi.fn();
  containPixel = vi.fn(() => true);
  convertFromPixel = vi.fn((_finder: unknown, _pixel: number[]) => [0, 0]);
  handlers: Record<string, Handler[]> = {};
  zr = new FakeZr();
  getZr = vi.fn(() => this.zr);
  on = vi.fn((t: string, h: Handler) => {
    (this.handlers[t] ??= []).push(h);
  });
  off = vi.fn((t: string, h: Handler) => {
    this.handlers[t] = (this.handlers[t] ?? []).filter((x) => x !== h);
  });
  emit(t: string, params: unknown) {
    (this.handlers[t] ?? []).forEach((h) => h(params));
  }
}

export const instances: FakeChart[] = [];
export const init = vi.fn(() => {
  const c = new FakeChart();
  instances.push(c);
  return c;
});
export const mockModule = {
  echarts: { init },
  CHART_THEME_NAME: "spareparts",
};
export const lastChart = () => instances[instances.length - 1];
export const resetEchartsMock = () => {
  instances.length = 0;
  init.mockClear();
};
