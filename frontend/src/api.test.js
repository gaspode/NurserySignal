import { afterEach, describe, expect, it, vi } from "vitest";
import { useApi } from "./api.js";

describe("API authentication failures", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("logs out only when an authenticated request receives a genuine 401", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: "unauthorized" }), {
          status: 401,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    const onUnauthorized = vi.fn();
    const request = useApi(async () => "token", onUnauthorized);

    await expect(request("/admin/signals")).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });
});
