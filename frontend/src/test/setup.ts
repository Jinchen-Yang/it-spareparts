// vitest + jsdom 的 antd 运行时垫片：antd v5 依赖 matchMedia / ResizeObserver /
// getComputedStyle 等浏览器 API，jsdom 缺省没有或不完整。
import "@testing-library/jest-dom/vitest";

if (!window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

if (!(window as any).ResizeObserver) {
  (window as any).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// Node 22+ 自带实验性 webstorage 全局（Node 23+ 默认开启），会占住 globalThis.localStorage；
// vitest 的 jsdom 环境不覆盖已存在的全局键，于是裸 localStorage 解析到 Node 内建实现
// （无 --localstorage-file 时不可用，甚至为 undefined）而非 jsdom 的 Storage。
// 该环境下 window / document.defaultView 就是被填充的 Node 全局，jsdom 原生 Storage
// 已不可达，因此用内存实现钉死这两个全局键，保证各 Node 版本行为一致。
// 仅覆盖代码里实际使用的标准方法；不支持 localStorage.foo 这类属性式存取。
class MemoryStorage {
  private store = new Map<string, string>();
  get length() { return this.store.size; }
  key(i: number) { return [...this.store.keys()][i] ?? null; }
  getItem(k: string) { return this.store.get(String(k)) ?? null; }
  setItem(k: string, v: string) { this.store.set(String(k), String(v)); }
  removeItem(k: string) { this.store.delete(String(k)); }
  clear() { this.store.clear(); }
}
for (const key of ["localStorage", "sessionStorage"] as const) {
  Object.defineProperty(globalThis, key, {
    configurable: true,
    writable: true,
    value: new MemoryStorage(),
  });
}

// rc-table 会把伪元素参数传给 getComputedStyle；jsdom 虽然实现了这个方法，
// 但伪元素分支只会向 stderr 打 "Not implemented"。测试不依赖伪元素样式，
// 因此保留真实元素的计算结果并忽略第二个参数，避免噪音淹没真正的 warning。
const nativeGetComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (element: Element) => nativeGetComputedStyle(element);
