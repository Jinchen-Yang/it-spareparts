import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import MaintenanceDownloadsCompatRedirect from "../MaintenanceDownloadsCompatRedirect";

function LocationProbe() {
  const location = useLocation();
  return <div>{`${location.pathname}${location.search}${location.hash}`}</div>;
}

afterEach(cleanup);

describe("MaintenanceDownloadsCompatRedirect", () => {
  it("旧下载地址自动跳到更新页并完整保留查询参数", async () => {
    render(
      <MemoryRouter initialEntries={[
        "/maintenance/downloads?from=reminders&contract=XSDD-001&project_id=project-1#upload",
      ]}>
        <Routes>
          <Route
            path="/maintenance/downloads"
            element={<MaintenanceDownloadsCompatRedirect />}
          />
          <Route path="/maintenance/updates" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText(
      "/maintenance/updates?from=reminders&contract=XSDD-001&project_id=project-1#upload",
    )).toBeInTheDocument();
  });
});
