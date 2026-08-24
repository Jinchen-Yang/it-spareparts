import { afterEach, describe, expect, it, vi } from "vitest";

import { saveBlob } from "../maintenanceWorkbooks";

describe("maintenanceWorkbooks.saveBlob", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("click 时锚点仍在 DOM，并延迟释放 object URL", () => {
    vi.useFakeTimers();
    const createObjectURL = vi.fn(() => "blob:test-download");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: createObjectURL,
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: revokeObjectURL,
    });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        expect(document.body.contains(this)).toBe(true);
        expect(this.download).toBe("验收.xlsx");
      });

    saveBlob(new Blob(["xlsx"]), "验收.xlsx");

    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(click).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).not.toHaveBeenCalled();
    vi.advanceTimersByTime(999);
    expect(revokeObjectURL).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:test-download");
  });
});
