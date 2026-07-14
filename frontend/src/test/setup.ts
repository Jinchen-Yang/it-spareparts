// vitest + jsdom 的 antd 运行时垫片：antd v5 依赖 matchMedia / ResizeObserver /
// getComputedStyle 等浏览器 API，jsdom 缺省没有或不完整。
import "@testing-library/jest-dom/vitest";

// Node ≥22 自带实验性 localStorage/sessionStorage 全局（未配 --localstorage-file 时
// 值为 undefined），且该自有属性会遮蔽 jsdom 注入的实现 → 测试里裸引用 localStorage
// 变 undefined（Node 26 实测命中）。jsdom 下 window === globalThis，不能转发回 window
// （会自引用），直接装一个符合 Storage 语义的内存实现。旧版 Node（CI）无该属性，不生效。
function memStorage(): Storage {
  const store = new Map<string, string>();
  return {
    get length() { return store.size; },
    clear: () => store.clear(),
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    removeItem: (k: string) => { store.delete(k); },
    setItem: (k: string, v: string) => { store.set(k, String(v)); },
  };
}
for (const key of ["localStorage", "sessionStorage"] as const) {
  if (globalThis[key] == null) {
    Object.defineProperty(globalThis, key, { configurable: true, value: memStorage() });
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
