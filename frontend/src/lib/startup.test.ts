import { beforeEach, describe, expect, it } from "vitest";
import { DEFAULT_SETTINGS, loadSettings } from "./settings";
import { applyStartupLink, parseStartupLink, startupSettings } from "./startup";

function link(payload: object): string {
  const bytes = new TextEncoder().encode(JSON.stringify(payload));
  let binary = "";
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return `#token=${btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")}`;
}

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, "", "/");
});

describe("startup link", () => {
  it("parses the tunnel URL and token", () => {
    const parsed = parseStartupLink(link({ u: "https://abc.trycloudflare.com/", m: "/mcp", c: "client-1", r: "rt-1" }));
    expect(parsed).toEqual({
      serverUrl: "https://abc.trycloudflare.com",
      mcpPath: "/mcp",
      clientId: "client-1",
      refreshToken: "rt-1",
    });
  });

  it("defaults the path and rejects malformed links", () => {
    expect(parseStartupLink(link({ u: "http://localhost:18000", c: "c", r: "r" }))?.mcpPath).toBe("/mcp");
    expect(parseStartupLink("#token=not base64")).toBeNull();
    expect(parseStartupLink("#token=" + btoa("not json"))).toBeNull();
    expect(parseStartupLink(link({ u: "https://x.test", c: "c" }))).toBeNull();
    expect(parseStartupLink(link({ u: "ftp://x.test", c: "c", r: "r" }))).toBeNull();
    expect(parseStartupLink("")).toBeNull();
  });

  it("wins over stored credentials and clears the manual bearer token", () => {
    const base = { ...DEFAULT_SETTINGS, bearerToken: "manual", pollMs: 2000, refreshToken: "old" };
    const merged = startupSettings(
      { serverUrl: "https://new.test", mcpPath: "/mcp", clientId: "c2", refreshToken: "r2" },
      base,
    );
    expect(merged.serverUrl).toBe("https://new.test");
    expect(merged.clientId).toBe("c2");
    expect(merged.refreshToken).toBe("r2");
    expect(merged.bearerToken).toBe("");
    expect(merged.pollMs).toBe(2000);
  });

  it("stores the settings and strips the fragment from the address bar", () => {
    const applied = applyStartupLink(link({ u: "https://new.test", c: "c2", r: "r2" }));
    expect(applied?.serverUrl).toBe("https://new.test");
    expect(window.location.hash).toBe("");
    const stored = loadSettings();
    expect(stored.serverUrl).toBe("https://new.test");
    expect(stored.refreshToken).toBe("r2");
  });

  it("does nothing without a link", () => {
    expect(applyStartupLink("")).toBeNull();
    expect(loadSettings().serverUrl).toBe(DEFAULT_SETTINGS.serverUrl);
  });
});
