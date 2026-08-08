import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import MaintenanceRemindersCompatRedirect from "../MaintenanceRemindersCompatRedirect";

function LocationProbe() {
  const location = useLocation();
  return <div>{`${location.pathname}${location.search}${location.hash}`}</div>;
}

afterEach(cleanup);

describe("MaintenanceRemindersCompatRedirect", () => {
  it("旧提醒地址进入项目方块面板并保留查询参数", async () => {
    render(
      <MemoryRouter initialEntries={[
        "/maintenance/reminders?project_id=project-1#urgent",
      ]}>
        <Routes>
          <Route
            path="/maintenance/reminders"
            element={<MaintenanceRemindersCompatRedirect />}
          />
          <Route path="/maintenance/projects" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText(
      "/maintenance/projects?project_id=project-1&reminder=all#urgent",
    )).toBeInTheDocument();
  });
});
