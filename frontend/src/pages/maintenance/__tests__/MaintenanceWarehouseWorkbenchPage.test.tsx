import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
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
        batch: {
          import_id: "SYN-IMPORT",
          filename: "synthetic-warehouse.xlsx",
          source_file_hash: "a".repeat(64),
          adapter_key: "shipment",
          adapter_version: "shipment_v1",
          version_state: "known",
          header_signature: "b".repeat(64),
          header_pairs: [],
          header_diff: {
            state: "approved_exact",
            baseline_signature: "b".repeat(64),
            added: [],
            removed: [],
            moved: [],
            label_changed: [],
          },
          applied_by: "synthetic-admin",
          applied_at: "2026-08-09T00:00:00Z",
        },
        links: [],
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
    expect(screen.getByRole("button", { name: "查看证据" })).toBeInTheDocument();
    expect(screen.queryByText("导入仓库导出文件")).toBeNull();
    expect(screen.getByText("本工作台不会修改库存、成本或返还率")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "单据事实 1" }));
    fireEvent.click(await screen.findByRole("button", { name: "查看证据" }));
    expect(screen.getByText("仓库单据证据")).toBeInTheDocument();
    expect(screen.getByText("synthetic-warehouse.xlsx")).toBeInTheDocument();
    expect(screen.getByText("a".repeat(64))).toBeInTheDocument();
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

  it("ignores an older search generation that finishes after the latest request", async () => {
    localStorage.setItem("role", "admin");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      action_maintenance_warehouse_manage: true,
    }));
    let resolveOldDocuments!: (value: unknown) => void;
    let resolveOldAmbiguities!: (value: unknown) => void;
    const oldDocuments = new Promise((resolve) => { resolveOldDocuments = resolve; });
    const oldAmbiguities = new Promise((resolve) => { resolveOldAmbiguities = resolve; });
    searchWarehouseDocuments.mockReset()
      .mockImplementationOnce(() => oldDocuments)
      .mockResolvedValue({
        data: {
          items: [{
            document_id: "LATEST-DOC-ID",
            document_type: "shipment",
            source_document_id: "LATEST-SOURCE-ID",
            document_no: "LATEST-DOCUMENT",
            document_date: "2026-08-09",
            raw_status: "已完成",
            normalized_status: "confirmed",
            line_count: 1,
            open_ambiguity_count: 0,
            batch: null,
            links: [],
          }],
          total: 1,
          page: 1,
          page_size: 50,
        },
      });
    searchWarehouseAmbiguities.mockReset()
      .mockImplementationOnce(() => oldAmbiguities)
      .mockResolvedValue({
        data: { items: [], total: 0, page: 1, page_size: 50 },
      });

    render(<MaintenanceWarehouseWorkbenchPage />);
    await vi.waitFor(() => {
      expect(searchWarehouseDocuments).toHaveBeenCalledTimes(1);
      expect(searchWarehouseAmbiguities).toHaveBeenCalledTimes(1);
    });
    fireEvent.change(screen.getByLabelText("搜索仓库单据"), {
      target: { value: "latest" },
    });
    fireEvent.click(screen.getByRole("button", { name: /搜\s*索/ }));
    await vi.waitFor(() => {
      expect(searchWarehouseDocuments).toHaveBeenCalledTimes(2);
      expect(searchWarehouseAmbiguities).toHaveBeenCalledTimes(2);
    });
    fireEvent.click(await screen.findByRole("tab", { name: "单据事实 1" }));
    expect(await screen.findByText("LATEST-DOCUMENT")).toBeInTheDocument();

    await act(async () => {
      resolveOldDocuments({
        data: {
          items: [{
            document_id: "STALE-DOC-ID",
            document_type: "shipment",
            source_document_id: "STALE-SOURCE-ID",
            document_no: "STALE-DOCUMENT",
            document_date: "2026-08-01",
            raw_status: "已完成",
            normalized_status: "confirmed",
            line_count: 1,
            open_ambiguity_count: 0,
            batch: null,
            links: [],
          }],
          total: 1,
          page: 1,
          page_size: 50,
        },
      });
      resolveOldAmbiguities({
        data: { items: [], total: 0, page: 1, page_size: 50 },
      });
      await Promise.resolve();
    });

    expect(screen.queryByText("STALE-DOCUMENT")).toBeNull();
    expect(screen.getByText("LATEST-DOCUMENT")).toBeInTheDocument();
  });
});
