import { serverEndpoint, type Settings } from "./settings";

/**
 * Access-token renewal for OAuth servers (the hosted ChordSmith).
 *
 * The manager keeps a refresh token (Settings) and silently exchanges it for short-lived
 * access tokens: the token endpoint rotates the refresh token on every use, so the new one is
 * handed back to the caller, which persists it. Access tokens are cached until shortly before
 * they expire. A manual bearer token always wins over the refresh flow.
 */

export interface TokenCache {
  refreshKey: string;
  accessToken: string;
  expiresAt: number;
}

export interface FreshToken {
  accessToken: string;
  expiresAt: number;
  refreshToken?: string;
}

const CACHE_KEY = "chordsmith-manager.token";
/** Renew this long before the access token actually expires. */
export const RENEW_MARGIN_MS = 60_000;

export function cacheKey(refreshToken: string): string {
  return refreshToken.slice(0, 12);
}

export function loadTokenCache(refreshToken: string, storage: Storage = localStorage): TokenCache | null {
  try {
    const raw = storage.getItem(CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<TokenCache>;
    if (
      typeof parsed.accessToken !== "string" ||
      typeof parsed.expiresAt !== "number" ||
      parsed.refreshKey !== cacheKey(refreshToken)
    ) {
      return null;
    }
    return parsed as TokenCache;
  } catch {
    return null;
  }
}

export function saveTokenCache(cache: TokenCache, storage: Storage = localStorage): void {
  storage.setItem(CACHE_KEY, JSON.stringify(cache));
}

export function hasRefreshCredentials(settings: Settings): boolean {
  return Boolean(settings.refreshToken && settings.clientId);
}

/** A cached access token that is still valid for a while, or null. */
export function cachedAccessToken(settings: Settings, storage: Storage = localStorage): string | null {
  const cached = loadTokenCache(settings.refreshToken, storage);
  if (cached && cached.expiresAt - Date.now() > RENEW_MARGIN_MS) return cached.accessToken;
  return null;
}

export async function refreshAccessToken(settings: Settings, fetcher: typeof fetch = fetch): Promise<FreshToken> {
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: settings.refreshToken,
    client_id: settings.clientId,
  });
  const response = await fetcher(serverEndpoint(settings, "/token"), {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(
      `Token refresh failed: HTTP ${response.status} ${text.slice(0, 200)}` +
        (text.includes("invalid_grant") ? " — paste a fresh refresh token in Settings." : ""),
    );
  }
  const data = (await response.json()) as { access_token?: string; expires_in?: number; refresh_token?: string };
  if (!data.access_token) throw new Error("Token refresh failed: no access_token in the response.");
  return {
    accessToken: data.access_token,
    expiresAt: Date.now() + (data.expires_in ?? 3600) * 1000,
    refreshToken: data.refresh_token,
  };
}
