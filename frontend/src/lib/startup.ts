import { loadSettings, saveSettings, type Settings } from "./settings";

/**
 * Startup handoff from scripts/start-stack.ps1.
 *
 * A quick tunnel gets a new hostname on every start, and a fresh monitor token is minted for
 * it. Instead of copying values out of monitor-token.ui.json, the start script opens the
 * manager at ``#token=<base64url(JSON)>``; the UI adopts the server URL and token, then removes
 * the fragment so the secret does not linger in the address bar or history. The hash is never
 * sent to a server.
 */

/** One-shot link payload; the short keys keep the URL compact. */
interface StartupToken {
  /** Server URL (the public tunnel URL). */
  u?: unknown;
  /** MCP path. */
  m?: unknown;
  /** OAuth client id. */
  c?: unknown;
  /** OAuth refresh token. */
  r?: unknown;
}

export interface StartupLink {
  serverUrl: string;
  mcpPath: string;
  clientId: string;
  refreshToken: string;
}

function decodeBase64Url(text: string): string {
  const base64 = text.replace(/-/g, "+").replace(/_/g, "/");
  const padding = base64.length % 4;
  const binary = atob(padding ? base64 + "=".repeat(4 - padding) : base64);
  return new TextDecoder().decode(Uint8Array.from(binary, (char) => char.charCodeAt(0)));
}

/** Parse ``#token=...``; null when the fragment is absent or malformed. */
export function parseStartupLink(hash: string): StartupLink | null {
  const match = /^#token=([A-Za-z0-9_-]+)$/.exec(hash);
  if (!match) return null;
  try {
    const parsed = JSON.parse(decodeBase64Url(match[1])) as StartupToken;
    if (typeof parsed.u !== "string" || typeof parsed.c !== "string" || typeof parsed.r !== "string") return null;
    if (!/^https?:\/\//.test(parsed.u) || !parsed.u) return null;
    return {
      serverUrl: parsed.u.replace(/\/+$/, ""),
      mcpPath: typeof parsed.m === "string" && parsed.m.startsWith("/") ? parsed.m : "/mcp",
      clientId: parsed.c,
      refreshToken: parsed.r,
    };
  } catch {
    return null;
  }
}

/** The settings a startup link asks for: its URL and token win, a manual bearer token is cleared. */
export function startupSettings(link: StartupLink, base: Settings): Settings {
  return {
    ...base,
    serverUrl: link.serverUrl,
    mcpPath: link.mcpPath,
    clientId: link.clientId,
    refreshToken: link.refreshToken,
    bearerToken: "",
  };
}

/**
 * Apply a startup link (when present) and strip it from the address bar. Call before the app
 * mounts so the connection uses the new settings on the first render.
 */
export function applyStartupLink(hash: string = window.location.hash): Settings | null {
  const link = parseStartupLink(hash);
  if (!link) return null;
  const next = startupSettings(link, loadSettings());
  saveSettings(next);
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  return next;
}
