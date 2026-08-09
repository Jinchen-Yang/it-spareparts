import { beforeEach, describe, expect, it, vi } from "vitest";
import axios from "axios";

const get = vi.fn();
const post = vi.fn();

vi.mock("../../api", () => ({
  api: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}));

import {
  assignMaintenanceSourceOrders,
  listMaintenanceSourceOrders,
  maintenanceSourceOrderParamsSerializer,
  unassignMaintenanceSourceOrders,
} from "../maintenanceSourceAssignments";

beforeEach(() => vi.clearAllMocks());

describe("maintenance source-order assignment API", () => {
  it("forwards independent directory filters and pagination", () => {
    listMaintenanceSourceOrders({
      q: "WBDD-001",
      source_order_id: ["source-1", "source-2"],
      assignment_status: "assigned",
      project_id: "project-1",
      page: 3,
      page_size: 50,
    });

    expect(get).toHaveBeenCalledWith(
      "/maintenance/project-assignments/orders",
      {
        params: {
          q: "WBDD-001",
          source_order_id: ["source-1", "source-2"],
          assignment_status: "assigned",
          project_id: "project-1",
          page: 3,
          page_size: 50,
        },
        paramsSerializer: maintenanceSourceOrderParamsSerializer,
      },
    );
  });

  it("serializes source-order IDs as repeated FastAPI query keys", () => {
    const uri = axios.getUri({
      url: "/maintenance/project-assignments/orders",
      params: { source_order_id: ["source-1", "source-2"] },
      paramsSerializer: maintenanceSourceOrderParamsSerializer,
    });

    expect(uri).toContain("source_order_id=source-1&source_order_id=source-2");
    expect(uri).not.toContain("source_order_id%5B%5D");
  });

  it("forwards optimistic assignment and unassignment expectations unchanged", () => {
    assignMaintenanceSourceOrders({
      project_id: "project-2",
      items: [{
        source_order_id: "source-1",
        expected_assignment_id: "assignment-1",
        expected_version: 4,
      }],
      reason: "人工改派",
    });
    unassignMaintenanceSourceOrders({
      items: [{ assignment_id: "assignment-2", expected_version: 7 }],
      reason: "人工撤销",
    });

    expect(post).toHaveBeenNthCalledWith(
      1,
      "/maintenance/project-assignments/orders/assign",
      {
        project_id: "project-2",
        items: [{
          source_order_id: "source-1",
          expected_assignment_id: "assignment-1",
          expected_version: 4,
        }],
        reason: "人工改派",
      },
    );
    expect(post).toHaveBeenNthCalledWith(
      2,
      "/maintenance/project-assignments/orders/unassign",
      {
        items: [{ assignment_id: "assignment-2", expected_version: 7 }],
        reason: "人工撤销",
      },
    );
  });
});
