import { describe, expect, it } from "vitest";
import type { Settings } from "./settings";
import {
  cacheKey,
  cachedAccessToken,
  hasRefreshCredentials,
  invalidateAccessToken,
  isAuthError,
  loadTokenCache,
  refreshAccessToken,
  saveTokenCache,
  tokenCacheEntry,
  withAuthRetry,
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

  it("follows the rotated refresh token so the cache keeps matching", () => {
    const storage = memoryStorage();
    const fresh = { accessToken: "at-2", expiresAt: Date.now() + 3600_000, refreshToken: "rt-rotated" };
    saveTokenCache(tokenCacheEntry(fresh, SETTINGS.refreshToken), storage);
    expect(cachedAccessToken({ ...SETTINGS, refreshToken: "rt-rotated" }, storage)).toBe("at-2");
    expect(cachedAccessToken(SETTINGS, storage)).toBeNull(); // the used-up token must not match
  });

  it("keeps the current key when the server does not rotate", () => {
    const storage = memoryStorage();
    const fresh = { accessToken: "at-3", expiresAt: Date.now() + 3600_000 };
    saveTokenCache(tokenCacheEntry(fresh, SETTINGS.refreshToken), storage);
    expect(cachedAccessToken(SETTINGS, storage)).toBe("at-3");
  });
});

describe("auth errors and retry", () => {
  it("recognises the server's rejection messages", () => {
    expect(isAuthError(new Error("/mcp: HTTP 401 unauthorized"))).toBe(true);
    expect(isAuthError(new Error('{"error":"invalid_token"}'))).toBe(true);
    expect(isAuthError(new Error("Authentication required"))).toBe(true);
    expect(isAuthError(new Error("HTTP 500 boom"))).toBe(false);
    expect(isAuthError("some string error")).toBe(false);
  });

  it("clears the cached access token", () => {
    const storage = memoryStorage();
    saveTokenCache(
      { refreshKey: cacheKey(SETTINGS.refreshToken), accessToken: "at", expiresAt: Date.now() + 3600_000 },
      storage,
    );
    expect(cachedAccessToken(SETTINGS, storage)).toBe("at");
    invalidateAccessToken(storage);
    expect(cachedAccessToken(SETTINGS, storage)).toBeNull();
  });

  it("retries once with a forced token after a 401", async () => {
    const forced: (boolean | undefined)[] = [];
    const used: (string | undefined)[] = [];
    const result = await withAuthRetry(
      async (force) => {
        forced.push(force);
        return force ? "fresh" : "stale";
      },
      async (token) => {
        used.push(token);
        if (token === "stale") throw new Error("/api/status: HTTP 401 invalid_token");
        return "ok";
      },
    );
    expect(result).toBe("ok");
    expect(forced).toEqual([false, true]);
    expect(used).toEqual(["stale", "fresh"]);
  });

  it("does not retry non-auth failures", async () => {
    let calls = 0;
    await expect(
      withAuthRetry(
        async () => "token",
        async () => {
          calls += 1;
          throw new Error("/api/status: HTTP 500 boom");
        },
      ),
    ).rejects.toThrow(/500/);
    expect(calls).toBe(1);
  });
});
