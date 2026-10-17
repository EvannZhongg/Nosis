// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import type { CacheUsage, ContextWindow } from "@nosis/protocol";
import { ContextWindowIndicator } from "../src/chat/RuntimeIndicators/ContextWindow";

const window: ContextWindow = {
  input_tokens: 120, max_input_tokens: 900, max_context_tokens: 1000,
  output_reserve_tokens: 100, compression_threshold: 800,
  compression_count: 0, compression_summary: null,
};

describe("context window cache usage", () => {
  it.each<[number | null, number | null, string]>([
    [100, 80, "80.0%"],
    [3, 1, "33.3%"],
    [1000, 995, "99.5%"],
    [1000, 999, "99.9%"],
    [100, 100, "100.0%"],
    [100, 0, "0.0%"],
    [100, null, "未知"],
    [null, 80, "未知"],
    [0, 0, "未知"],
  ])("shows reported input %s and cache reads %s as %s", async (input, cached, expected) => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const cacheUsage: CacheUsage | null = input === null || cached === null ? null : {
      input_tokens: input, cache_read_tokens: cached,
    };
    try {
      await act(async () => root.render(<ContextWindowIndicator window={window} cacheUsage={cacheUsage} />));
      await act(async () => container.querySelector("button")!.click());
      const row = Array.from(container.querySelectorAll("dl > div"))
        .find((element) => element.querySelector("dt")?.textContent === "缓存命中")!;
      expect(row.querySelector("dd")?.textContent).toBe(expected);
      await act(async () => root.render(<ContextWindowIndicator window={window} cacheUsage={null} />));
      expect(row.querySelector("dd")?.textContent).toBe("未知");
    } finally {
      await act(async () => root.unmount());
      container.remove();
      vi.unstubAllGlobals();
    }
  });
});
