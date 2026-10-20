import type { ReactNode } from "react";
import { MemoryRouter, useLocation, useNavigate, useNavigationType } from "react-router-dom";
import { renderWithoutMotion } from "./antdRender";

/** 观察真实 MemoryRouter 的公开导航接口，不 mock URL hook。 */
export function renderUrlPage(page: ReactNode, entry: string) {
  const router = {} as {
    state: { location: ReturnType<typeof useLocation>; historyAction: ReturnType<typeof useNavigationType> };
    navigate: ReturnType<typeof useNavigate>;
  };
  function NavigationProbe() {
    router.state = { location: useLocation(), historyAction: useNavigationType() };
    router.navigate = useNavigate();
    return null;
  }
  const view = renderWithoutMotion(
    <MemoryRouter initialEntries={["/before", entry]} initialIndex={1}>
      <NavigationProbe />{page}
    </MemoryRouter>,
  );
  return { router, ...view };
}
