import { afterEach, describe, expect, it, vi } from "vitest";
import { bearerFetch } from "./mcp";

interface Captured {
  url: string;
  headers: Headers;
}

function captureFetch(): Captured[] {
  const calls: Captured[] = [];
  vi.stubGlobal(
    "fetch",
    (async (url: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(url), headers: new Headers(init?.headers) });
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }) as typeof fetch,
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("bearerFetch", () => {
  it("resolves the token again on every request", async () => {
    const calls = captureFetch();
    let token = "at-1";
    const send = bearerFetch({}, async () => token);
    await send("https://example.test/mcp", { method: "POST", headers: { "content-type": "application/json" } });
    token = "at-2";
    await send("https://example.test/mcp", { method: "GET" });
    expect(calls[0].headers.get("authorization")).toBe("Bearer at-1");
    expect(calls[0].headers.get("content-type")).toBe("application/json");
    expect(calls[1].headers.get("authorization")).toBe("Bearer at-2");
  });

  it("keeps base headers and drops the authorization when there is no token", async () => {
    const calls = captureFetch();
    const send = bearerFetch({ "x-base": "yes" }, async () => undefined);
    await send("https://example.test/mcp", { headers: { authorization: "Bearer used-up" } });
    expect(calls[0].headers.get("authorization")).toBeNull();
    expect(calls[0].headers.get("x-base")).toBe("yes");
  });

  it("hands a manual token through untouched", async () => {
    const calls = captureFetch();
    const send = bearerFetch({ Authorization: "Bearer manual" });
    await send("https://example.test/mcp");
    expect(calls[0].headers.get("authorization")).toBe("Bearer manual");
  });

  it("records the last token for the pagehide cleanup", async () => {
    captureFetch();
    const seen: { token?: string } = {};
    await bearerFetch({}, async () => "at-9", seen)("https://example.test/mcp", { method: "DELETE" });
    expect(seen.token).toBe("at-9");
  });
});
