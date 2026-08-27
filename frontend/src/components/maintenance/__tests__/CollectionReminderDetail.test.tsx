import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import type { CollectionProjectDetailResponse } from "../../../api/maintenanceCollectionReminders";
import CollectionReminderDetail from "../CollectionReminderDetail";
import type { MaintenanceCapabilities } from "../maintenancePermissions";

afterEach(cleanup);

const capabilities = {
  canFollowUpCollection: false,
  canImportCollectionPlan: false,
} as MaintenanceCapabilities;

const detail: CollectionProjectDetailResponse = {
  project: {
    project_id: "project/含空格",
    project_code: "PM-1",
    display_name: "维保项目",
    lifecycle_status: "ongoing",
    version: 1,
    manager_assignment: { username: null, display_name: null },
    service_period: {
      service_start: "2026-01-01",
      service_end: "2026-12-31",
      completeness_state: "complete",
    },
    contracts: [],
  },
  summary: {
    total: 0,
    needs_review: 0,
    handled: 0,
    incomplete: 0,
    overdue: 0,
    due_this_month: 0,
    upcoming: 0,
  },
  rows: [],
  as_of: "2026-08-27",
  data_version: "test",
  amount_visibility: "visible",
};

it("查看完整项目使用正式详情路由并安全编码 project_id", () => {
  render(
    <MemoryRouter>
      <CollectionReminderDetail
        detail={detail}
        loading={false}
        error={false}
        selected
        capabilities={capabilities}
        onFollowUp={vi.fn()}
        onImportPlan={vi.fn()}
        onRetry={vi.fn()}
      />
    </MemoryRouter>,
  );

  expect(screen.getByRole("link", { name: "查看完整项目" }))
    .toHaveAttribute("href", "/maintenance/projects/project%2F%E5%90%AB%E7%A9%BA%E6%A0%BC");
});
