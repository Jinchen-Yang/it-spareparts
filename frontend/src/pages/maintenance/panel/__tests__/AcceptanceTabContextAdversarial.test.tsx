import { Suspense, startTransition, useState } from "react";
import { Modal } from "antd";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../../../../test/antdRender";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getMaintenanceAcceptanceChecklist: vi.fn(),
  downloadAcceptanceChecklistTemplate: vi.fn(),
  previewMaintenanceAcceptanceChecklist: vi.fn(),
  applyMaintenanceAcceptanceChecklist: vi.fn(),
  saveBlob: vi.fn(),
}));
vi.mock("../../../../api/maintenanceOperations", async () => ({
  ...await vi.importActual<typeof import("../../../../api/maintenanceOperations")>("../../../../api/maintenanceOperations"),
  getMaintenanceAcceptanceChecklist: mocks.getMaintenanceAcceptanceChecklist,
  downloadAcceptanceChecklistTemplate: mocks.downloadAcceptanceChecklistTemplate,
  previewMaintenanceAcceptanceChecklist: mocks.previewMaintenanceAcceptanceChecklist,
  applyMaintenanceAcceptanceChecklist: mocks.applyMaintenanceAcceptanceChecklist,
}));
vi.mock("../../../../api/maintenanceWorkbooks", async () => ({
  ...await vi.importActual<typeof import("../../../../api/maintenanceWorkbooks")>("../../../../api/maintenanceWorkbooks"), saveBlob: mocks.saveBlob,
}));
vi.mock("../../../../components/maintenance/MaintenanceAcceptancePanel", () => ({ default: () => null }));
import AcceptanceTab from "../AcceptanceTab";
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
const empty = { data: { current: null, history: [] } };
const preview = { data: {
  batch_id: "independent-batch", item_rows: 1, done_rows: 0, todo_rows: 1,
  issue_rows: 0, will_replace_rows: 0, issues: [],
} };
beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  localStorage.setItem("token", "independent-account-A");
  mocks.getMaintenanceAcceptanceChecklist.mockResolvedValue(empty);
});
afterEach(async () => {
  cleanup();
  await act(async () => { Modal.destroyAll(); });
  vi.restoreAllMocks();
  localStorage.clear();
});
describe("AcceptanceTab independent account boundary cases", () => {
  it("an uncommitted permission render does not disable the still-visible authorized input", async () => {
    const never = new Promise<void>(() => {});
    const suspended = vi.fn();
    mocks.previewMaintenanceAcceptanceChecklist.mockReturnValue(never);
    function Block({ active }: { active: boolean }) {
      if (active) { suspended(); throw never; }
      return null;
    }
    function Harness() {
      const [revoked, setRevoked] = useState(false);
      return <>
        <button onClick={() => startTransition(() => setRevoked(true))}>Try revoke</button>
        <Suspense fallback={<p>Uncommitted permission</p>}>
          <AcceptanceTab projectId="A" canImport={!revoked} />
          <Block active={revoked} />
        </Suspense>
      </>;
    }
    const view = render(<Harness />);
    await screen.findByText(/尚未导入验收清单/);
    await act(async () => { fireEvent.click(screen.getByText("Try revoke")); });
    expect(suspended).toHaveBeenCalled();
    expect(screen.queryByText("Uncommitted permission")).not.toBeInTheDocument();
    await act(async () => {
      fireEvent.change(view.container.querySelector('input[type="file"]')!, {
        target: { files: [new File(["xlsx"], "committed-account-file.xlsx")] },
      });
    });
    expect(mocks.previewMaintenanceAcceptanceChecklist).toHaveBeenCalledOnce();
  });
  it.each(["upload", "download"])("old rendered %s handler must not start a request under a newly stored account", async (operation) => {
    mocks.previewMaintenanceAcceptanceChecklist.mockReturnValue(new Promise(() => {}));
    mocks.downloadAcceptanceChecklistTemplate.mockReturnValue(new Promise(() => {}));
    const view = render(<AcceptanceTab projectId="A" canImport />);
    await screen.findByText(/尚未导入验收清单/);
    // Storage changes synchronously; new account permissions have not committed to the UI.
    localStorage.setItem("token", "independent-account-B");
    await act(async () => {
      if (operation === "upload") fireEvent.change(view.container.querySelector('input[type="file"]')!, {
        target: { files: [new File(["xlsx"], "old-account-file.xlsx")] },
      });
      else fireEvent.click(screen.getByRole("button", { name: /下载模板/ }));
    });
    expect(mocks.previewMaintenanceAcceptanceChecklist).not.toHaveBeenCalled();
    expect(mocks.downloadAcceptanceChecklistTemplate).not.toHaveBeenCalled();
    expect(mocks.applyMaintenanceAcceptanceChecklist).not.toHaveBeenCalled();
  });
  it("revoking upload permission does not discard an already requested read-only template", async () => {
    const pending = deferred<{ data: Blob }>();
    mocks.downloadAcceptanceChecklistTemplate.mockReturnValue(pending.promise);
    const view = render(<AcceptanceTab projectId="A" canImport />);
    await screen.findByText(/尚未导入验收清单/);
    fireEvent.click(screen.getByRole("button", { name: /下载模板/ }));
    view.rerender(<AcceptanceTab projectId="A" canImport={false} />);
    const blob = new Blob(["template"]);
    await act(async () => { pending.resolve({ data: blob }); });
    expect(mocks.saveBlob).toHaveBeenCalledWith(blob, "验收需求清单模板.xlsx");
    expect(mocks.saveBlob).toHaveBeenCalledOnce();
  });
  it("a new account render can import while the prior account preview is still unresolved", async () => {
    const old = deferred<typeof preview>();
    const current = deferred<typeof preview>();
    mocks.previewMaintenanceAcceptanceChecklist.mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise);
    const confirm = vi.spyOn(Modal, "confirm").mockReturnValue({ destroy: vi.fn(), update: vi.fn() });
    const view = render(<AcceptanceTab projectId="A" canImport />);
    await screen.findByText(/尚未导入验收清单/);
    const upload = () => fireEvent.change(view.container.querySelector('input[type="file"]')!, {
      target: { files: [new File(["xlsx"], "account-file.xlsx")] },
    });
    upload();
    await waitFor(() => expect(mocks.previewMaintenanceAcceptanceChecklist).toHaveBeenCalledOnce());
    localStorage.setItem("token", "independent-account-B");
    view.rerender(<AcceptanceTab projectId="A" canImport />);
    upload();
    await waitFor(() => expect(mocks.previewMaintenanceAcceptanceChecklist).toHaveBeenCalledTimes(2));
    await act(async () => { current.resolve(preview); });
    expect(confirm).toHaveBeenCalledOnce();
    await act(async () => { old.resolve(preview); });
    expect(confirm).toHaveBeenCalledOnce();
    await act(async () => { confirm.mock.calls[0][0].onCancel!(); });
    expect(mocks.applyMaintenanceAcceptanceChecklist).not.toHaveBeenCalled();
  });
});
