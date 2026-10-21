/**
 * 会话守卫（DL-010，含返工）：两层——
 * 1. 迟到守卫：请求发出前捕获当时 token，响应落地 / 下载触发前再读 localStorage
 *    直接比对（sessionStale），不只判断组件 mounted。账号切换、登出或 token 刷新后，
 *    迟到响应一律作废，不渲染另一会话的数据、不触发另一会话的下载。
 * 2. 所属会话守卫（本轮返工）：组件挂载时的 token 是其全部异步入口的 owner；
 *    发请求前直读 localStorage 与 owner 比对（useSessionOwner + sessionStale）。
 *    静默换 token（无重渲染、无 storage 事件）后，旧会话拥有的导出 / 下钻 / 取数
 *    一律拒发，并清理旧会话的金额 / 选项 / 排序 / 下载上下文——旧数据不冒充当前
 *    会话结果、旧按钮不以新身份提交。
 *
 * 直接读 localStorage（不经 React 状态）：权限/角色快照可能停留在旧渲染，token 是
 * 唯一随登录实时写入的会话真值（api.ts 请求头同源）。
 */
import { useRef } from "react";

export function readSessionToken(): string | null {
  try {
    return localStorage.getItem("token");
  } catch {
    return null;
  }
}

/** 落地前校验：发起时 token 与当前不一致 → true（过期响应，应丢弃）。 */
export function sessionStale(issuedWith: string | null): boolean {
  return readSessionToken() !== issuedWith;
}

/** 所属会话失效的用户可见文案——与「加载失败 / 无权限」分开（重试无济于事，
 * 刷新后以当前账号重新挂载才恢复）。 */
export const SESSION_EXPIRED_MESSAGE = "登录会话已变更（已切换账号或登出），请刷新页面后继续";

/**
 * 组件所属会话 owner（DL-010 返工）：挂载时 token，此后不随渲染更新——静默换
 * token 不触发重渲染，只能请求时直读比对。页级 owner 由页面捕获后下传给子视图 /
 * 抽屉，保证整页异步入口同一所属会话（后挂载的页签不会以新会话混取数据）。
 */
export function useSessionOwner(): string | null {
  return useRef(readSessionToken()).current;
}
