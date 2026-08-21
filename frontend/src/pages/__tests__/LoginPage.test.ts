import { afterEach, expect, it, vi } from "vitest";
import { persistLoginSession } from "../LoginPage";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

/**
 * 环境无关的写入顺序观测：
 * Node 20/22 下 jsdom 真身 Storage、Node ≥26 下 test/setup 的内存垫片，
 * 两者的 vi.spyOn（prototype 或实例）都拦不到调用——只能在测试期内
 * 用 defineProperty 整体换掉 globalThis.localStorage，直接记录写入顺序。
 */
function withWriteRecorder(run: () => void): {
  order: string[];
  store: Map<string, string>;
} {
  const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  const order: string[] = [];
  const store = new Map<string, string>();
  const fake: Storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    removeItem: (k: string) => {
      store.delete(k);
    },
    setItem: (k: string, v: string) => {
      store.set(k, String(v));
      order.push(k);
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: fake,
  });
  try {
    run();
  } finally {
    if (original) {
      Object.defineProperty(globalThis, "localStorage", original);
    } else {
      // eslint-disable-next-line @typescript-eslint/no-dynamic-delete
      delete (globalThis as Record<string, unknown>).localStorage;
    }
  }
  return { order, store };
}

it("登录会话最后发布 token，使其它标签页只看到完整权限快照", () => {
  const { order, store } = withWriteRecorder(() =>
    persistLoginSession({
      token: "signed-token",
      role: "admin",
      name: "管理员",
      permissions: { page_parts: true },
    }),
  );

  expect(order).toEqual([
    "role",
    "name",
    "permissions",
    "beta_features",
    "token",
  ]);
  expect(store.get("token")).toBe("signed-token");
});

it("新账号登录时清除旧版本遗留的个人税口径", () => {
  localStorage.setItem("maintenance_project_profit_basis", "ex");
  localStorage.setItem("tax_basis", "inc");

  const { store } = withWriteRecorder(() =>
    persistLoginSession({
      token: "account-b-token",
      role: "readonly",
      name: "账号 B",
      permissions: { page_maintenance: true },
    }),
  );

  expect(store.has("maintenance_project_profit_basis")).toBe(false);
  expect(store.has("tax_basis")).toBe(false);
});
