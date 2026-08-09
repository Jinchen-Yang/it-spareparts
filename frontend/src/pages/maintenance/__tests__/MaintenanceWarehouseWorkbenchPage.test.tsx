import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const {
  searchWarehouseAmbiguities,
  searchWarehouseDocuments,
} = vi.hoisted(() => ({
  searchWarehouseAmbiguities: vi.fn(),
  searchWarehouseDocuments: vi.fn(),
}));

vi.mock("../../../api/maintenanceWarehouse", () => ({
  applyWarehouseImport: vi.fn(),
  previewWarehouseImport: vi.fn(),
  resolveWarehouseAmbiguity: vi.fn(),
  searchWarehouseAmbiguities,
  searchWarehouseDocuments,
}));

import MaintenanceWarehouseWorkbenchPage from "../MaintenanceWarehouseWorkbenchPage";


beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  searchWarehouseDocuments.mockResolvedValue({
    data: {
      items: [{
        document_id: "SYN-DOC-ID",
        document_type: "shipment",
        source_document_id: "SYN-SOURCE-DOC",
        document_no: "SYN-SHIP-001",
        document_date: "2026-08-01",
        raw_status: "已完成",
        normalized_status: "confirmed",
        line_count: 1,
        open_ambiguity_count: 1,
      }],
      total: 1,
      page: 1,
      page_size: 50,
    },
  });
  searchWarehouseAmbiguities.mockResolvedValue({
    data: {
      items: [{
        ambiguity_id: "SYN-AMBIGUITY",
        import_id: "SYN-IMPORT",
        ambiguity_type: "unknown_version",
        field_code: null,
        source_row: null,
        status: "open",
        version: 1,
        candidates: [],
        resolution: null,
        resolution_reason: null,
        resolved_by: null,
        document: null,
      }],
      total: 1,
      page: 1,
      page_size: 50,
    },
  });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("MaintenanceWarehouseWorkbenchPage", () => {
  it("lets maintenance readers inspect facts and ambiguities without write controls", async () => {
    localStorage.setItem("role", "purchaser");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      action_maintenance_warehouse_manage: false,
    }));
    render(<MaintenanceWarehouseWorkbenchPage />);

    expect(await screen.findByText("未知模板版本")).toBeInTheDocument();
    expect(screen.getByText("只读")).toBeInTheDocument();
    expect(screen.queryByText("导入仓库导出文件")).toBeNull();
    expect(screen.getByText("本工作台不会修改库存、成本或返还率")).toBeInTheDocument();
  });

  it("shows preview/apply controls only with the explicit real-account capability", async () => {
    localStorage.setItem("role", "admin");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      action_maintenance_warehouse_manage: true,
    }));
    render(<MaintenanceWarehouseWorkbenchPage />);

    expect(await screen.findByText("导入仓库导出文件")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "零写入预览" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "人工裁决" })).toBeInTheDocument();
  });
});
