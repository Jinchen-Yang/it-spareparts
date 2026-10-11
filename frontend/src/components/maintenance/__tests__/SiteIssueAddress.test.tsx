import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import SiteIssueAddress from "../SiteIssueAddress";

afterEach(cleanup);

describe("SiteIssueAddress", () => {
  it("完整显示长地址和换行，并提供复制", () => {
    const address = "合成领用地址".repeat(30) + "\n机房 2 层";
    render(<SiteIssueAddress value={address} />);
    const value = screen.getByText(address, { normalizer: (text) => text });
    expect(value).toBeInTheDocument();
    expect(value.parentElement).toHaveStyle({ whiteSpace: "pre-wrap", overflowWrap: "anywhere" });
    expect(screen.getByRole("button", { name: /Copy|复制/ })).toBeInTheDocument();
    expect(screen.getByText("领用地址：")).toBeInTheDocument();
  });

  it.each([null, undefined, "", " \n "])("未填地址 %s 明确显示为空且不提供复制", (value) => {
    render(<SiteIssueAddress value={value} showLabel={false} />);
    expect(screen.getByText("未填写")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
