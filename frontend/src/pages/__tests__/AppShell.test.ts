import { describe, expect, it } from "vitest";
import { shellHeaderMode } from "../../AppShell";

describe("AppShell responsive header", () => {
  it("exactly 768px (md=true, lg=false) uses compact tablet header", () => {
    expect(shellHeaderMode({ md: true, lg: false })).toBe("tablet");
  });

  it("wide desktop and mobile retain their respective modes", () => {
    expect(shellHeaderMode({ md: true, lg: true })).toBe("desktop");
    expect(shellHeaderMode({ md: false, lg: false })).toBe("mobile");
    expect(shellHeaderMode({})).toBe("desktop"); // first frame avoids layout flash
  });
});
