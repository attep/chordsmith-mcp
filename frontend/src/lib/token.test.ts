import { describe, expect, it } from "vitest";
import type { Settings } from "./settings";
import {
  cacheKey,
  cachedAccessToken,
  hasRefreshCredentials,
  loadTokenCache,
  refreshAccessToken,
  saveTokenCache,
  RENEW_MARGIN_MS,
} from "./token";

const SETTINGS: Settings = {
  serverUrl: "https://example.test",
  mcpPath: "/mcp",
  bearerToken: "",
  clientId: "client-1",
  refreshToken: "refresh-abcdef123456",
  ollamaUrl: "http://localhost:11434",
  ollamaModel: "",
  pollMs: 1500,
};

function memoryStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (key) => map.get(key) ?? null,
    key: (index) => [...map.keys()][index] ?? null,
    removeItem: (key) => void map.delete(key),
    setItem: (key, value) => void map.set(key, String(value)),
  };
}

describe("refresh access token", () => {
  it("posts the refresh grant and returns the rotated tokens", async () => {
    const calls: { url: string; body: string }[] = [];
    const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), body: String(init?.body ?? "") });
      return new Response(
        JSON.stringify({ access_token: "at-1", refresh_token: "rt-2", expires_in: 3600 }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as typeof fetch;

    const fresh = await refreshAccessToken(SETTINGS, fetcher);
    expect(calls[0].url).toBe("https://example.test/token");
    expect(calls[0].body).toContain("grant_type=refresh_token");
    expect(calls[0].body).toContain("client_id=client-1");
    expect(calls[0].body).toContain("refresh_token=refresh-abcdef123456");
    expect(fresh.accessToken).toBe("at-1");
    expect(fresh.refreshToken).toBe("rt-2");
    expect(fresh.expiresAt).toBeGreaterThan(Date.now() + 3_000_000);
  });

  it("surfaces the server's error, including the invalid_grant hint", async () => {
    const fetcher = (async () =>
      new Response('{"error":"invalid_grant"}', { status: 400 })) as typeof fetch;
    await expect(refreshAccessToken(SETTINGS, fetcher)).rejects.toThrow(/invalid_grant/);
    await expect(refreshAccessToken(SETTINGS, fetcher)).rejects.toThrow(/fresh refresh token/);
  });

  it("rejects a response without an access token", async () => {
    const fetcher = (async () => new Response("{}", { status: 200 })) as typeof fetch;
    await expect(refreshAccessToken(SETTINGS, fetcher)).rejects.toThrow(/no access_token/);
  });
});

describe("token cache", () => {
  it("is keyed to the refresh token and honours the renewal margin", () => {
    const storage = memoryStorage();
    const settings = { ...SETTINGS, refreshToken: "rt-a-long-enough" };
    expect(hasRefreshCredentials(settings)).toBe(true);
    expect(cachedAccessToken(settings, storage)).toBeNull(); // nothing cached yet

    saveTokenCache(
      { refreshKey: cacheKey(settings.refreshToken), accessToken: "at", expiresAt: Date.now() + 3600_000 },
      storage,
    );
    expect(cachedAccessToken(settings, storage)).toBe("at");
    expect(loadTokenCache("rt-other", storage)).toBeNull(); // a different refresh token must not reuse it

    saveTokenCache(
      {
        refreshKey: cacheKey(settings.refreshToken),
        accessToken: "at-stale",
        expiresAt: Date.now() + RENEW_MARGIN_MS / 2,
      },
      storage,
    );
    expect(cachedAccessToken(settings, storage)).toBeNull(); // nearly expired: renew instead
  });

  it("treats missing credentials as no refresh support", () => {
    expect(hasRefreshCredentials({ ...SETTINGS, refreshToken: "" })).toBe(false);
    expect(hasRefreshCredentials({ ...SETTINGS, clientId: "" })).toBe(false);
  });
});
