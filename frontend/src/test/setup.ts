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

// rc-table 会把伪元素参数传给 getComputedStyle；jsdom 虽然实现了这个方法，
// 但伪元素分支只会向 stderr 打 "Not implemented"。测试不依赖伪元素样式，
// 因此保留真实元素的计算结果并忽略第二个参数，避免噪音淹没真正的 warning。
const nativeGetComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (element: Element) => nativeGetComputedStyle(element);
