// vitest + jsdom 的 antd 运行时垫片：antd v5 依赖 matchMedia / ResizeObserver /
// getComputedStyle 等浏览器 API，jsdom 缺省没有或不完整。
import "@testing-library/jest-dom/vitest";

// Node 22+ 在 globalThis 上自带 localStorage/sessionStorage getter（未配
// --localstorage-file 时恒返回 undefined），vitest populateGlobal 见"已存在"便跳过
// 注入 jsdom 实现，测试里 typeof localStorage === "undefined"（jsdom 原始 window
// 也被 Node getter 遮蔽，拿不回来）→ 用内存版 Storage 顶上。
// 旧版 Node（CI）没有该 getter，此分支不生效。
function memStorage(): Storage {
  let store = new Map<string, string>();
  return {
    get length() { return store.size; },
    clear: () => { store = new Map(); },
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    key: (i: number) => [...store.keys()][i] ?? null,
    removeItem: (k: string) => { store.delete(k); },
    setItem: (k: string, v: string) => { store.set(k, String(v)); },
  };
}
for (const key of ["localStorage", "sessionStorage"] as const) {
  if (typeof globalThis[key] === "undefined") {
    Object.defineProperty(globalThis, key,
      { value: memStorage(), writable: true, configurable: true });
  }
}

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

// rc-table 会把伪元素参数传给 getComputedStyle；jsdom 虽然实现了这个方法，
// 但伪元素分支只会向 stderr 打 "Not implemented"。测试不依赖伪元素样式，
// 因此保留真实元素的计算结果并忽略第二个参数，避免噪音淹没真正的 warning。
const nativeGetComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (element: Element) => nativeGetComputedStyle(element);
