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

// rc-table 在 jsdom 里读取 offsetParent 之类的布局属性，缺省即可
if (!("getComputedStyle" in window)) {
  (window as any).getComputedStyle = () => ({ getPropertyValue: () => "" });
}
