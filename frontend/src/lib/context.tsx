import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { extractJobIds, loadJobs, mergeJobs, saveJobs, type KnownJob } from "./jobs";
import { McpConnection, resultData, type CallToolResult, type Tool } from "./mcp";
import { loadSettings, saveSettings, SETTINGS_KEY, type Settings } from "./settings";
import {
  cachedAccessToken,
  hasRefreshCredentials,
  invalidateAccessToken,
  isAuthError,
  refreshAccessToken,
  saveTokenCache,
  tokenCacheEntry,
} from "./token";

export type ConnectionState = "connecting" | "connected" | "error";

interface AppContextValue {
  settings: Settings;
  updateSettings: (next: Settings) => void;
  connection: McpConnection | null;
  connectionState: ConnectionState;
  connectionError: string | null;
  tools: Tool[];
  reconnect: () => void;
  callTool: (name: string, args: Record<string, unknown>) => Promise<CallToolResult>;
  /** The bearer token to use right now: manual token, refreshed access token, or undefined. */
  getToken: (force?: boolean) => Promise<string | undefined>;
  jobs: KnownJob[];
  rememberJobs: (jobs: KnownJob[]) => void;
  forgetJob: (jobId: string) => void;
  clearJobs: () => void;
}

const AppContext = createContext<AppContextValue | null>(null);

const KEEPALIVE_MS = 15_000;

export function AppProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<Settings>(loadSettings);
  const [connection, setConnection] = useState<McpConnection | null>(null);
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [tools, setTools] = useState<Tool[]>([]);
  const [attempt, setAttempt] = useState(0);
  const [jobs, setJobs] = useState<KnownJob[]>(loadJobs);
  const connectionRef = useRef<McpConnection | null>(null);
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  const refreshInFlight = useRef<Promise<string | undefined> | null>(null);

  /** A fresh bearer token: the manual one, or a silent refresh against the token endpoint. */
  const getToken = useCallback(async (force = false): Promise<string | undefined> => {
    const current = settingsRef.current;
    if (current.bearerToken) return current.bearerToken;
    if (!hasRefreshCredentials(current)) return undefined;
    if (!force) {
      const cached = cachedAccessToken(current);
      if (cached) return cached;
    }
    if (!refreshInFlight.current) {
      refreshInFlight.current = refreshAccessToken(current)
        .then((fresh) => {
          // The server rotates the pair on every exchange: the cached entry must be keyed to the
          // refresh token the client now holds, or the cache can never match again and every
          // later request re-refreshes (rotating the chain on every poll).
          saveTokenCache(tokenCacheEntry(fresh, current.refreshToken));
          if (fresh.refreshToken && fresh.refreshToken !== settingsRef.current.refreshToken) {
            // the token endpoint rotates refresh tokens: keep the new one
            const next = { ...settingsRef.current, refreshToken: fresh.refreshToken };
            settingsRef.current = next; // publish at once: a request may read it before the re-render
            saveSettings(next);
            setSettings(next);
          }
          return fresh.accessToken;
        })
        .finally(() => {
          refreshInFlight.current = null;
        });
    }
    return refreshInFlight.current;
  }, []);

  useEffect(() => {
    let cancelled = false;
    setConnectionState("connecting");
    setConnectionError(null);
    // The connection resolves its bearer token per request, so background renewals need no
    // reconnect; only the explicit retry below forces a fresh token before reopening.
    const open = async (force: boolean) => {
      if (force) await getToken(true);
      return McpConnection.open(settings, getToken);
    };
    open(false)
      .catch(async (error: unknown) => {
        // a 401 may just mean the cached access token died with a server restart: refresh + retry
        if (!isAuthError(error)) throw error;
        invalidateAccessToken();
        return open(true);
      })
      .then(async (opened) => {
        if (cancelled) {
          void opened.close();
          return;
        }
        connectionRef.current = opened;
        setConnection(opened);
        setTools(await opened.listTools());
        setConnectionState("connected");
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setConnection(null);
        setTools([]);
        setConnectionState("error");
        const message = error instanceof Error ? error.message : String(error);
        setConnectionError(
          isAuthError(error) && settingsRef.current.bearerToken
            ? `${message} — the manual Bearer token was rejected; clear it in Settings to use the automatic refresh token.`
            : message,
        );
      });
    return () => {
      cancelled = true;
      const current = connectionRef.current;
      connectionRef.current = null;
      if (current) void current.close();
    };
  }, [settings.serverUrl, settings.mcpPath, settings.bearerToken, settings.clientId, attempt, getToken]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    // A second tab rotates the shared refresh token and writes it back to localStorage; follow
    // it instead of refreshing the used-up chain (which reads as a forced manual re-paste).
    // Only the credentials are adopted - the other settings stay per-tab.
    const onStorage = (event: StorageEvent) => {
      if (event.key !== SETTINGS_KEY) return;
      const stored = loadSettings();
      const current = settingsRef.current;
      if (stored.refreshToken === current.refreshToken && stored.bearerToken === current.bearerToken) return;
      const next = {
        ...current,
        bearerToken: stored.bearerToken,
        clientId: stored.clientId,
        refreshToken: stored.refreshToken,
      };
      settingsRef.current = next; // publish at once: a request may read it before the re-render
      setSettings(next);
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  useEffect(() => {
    if (!connection) return;
    const timer = window.setInterval(() => void connection.ping().catch(() => undefined), KEEPALIVE_MS);
    return () => window.clearInterval(timer);
  }, [connection]);

  useEffect(() => {
    const onPageHide = () => connectionRef.current?.abandon();
    window.addEventListener("pagehide", onPageHide);
    return () => window.removeEventListener("pagehide", onPageHide);
  }, []);

  const updateSettings = useCallback((next: Settings) => {
    saveSettings(next);
    setSettings(next);
  }, []);

  const rememberJobs = useCallback((incoming: KnownJob[]) => {
    if (!incoming.length) return;
    setJobs((current) => {
      const merged = mergeJobs(current, incoming);
      saveJobs(merged);
      return merged;
    });
  }, []);

  const forgetJob = useCallback((jobId: string) => {
    setJobs((current) => {
      const next = current.filter((job) => job.job_id !== jobId);
      saveJobs(next);
      return next;
    });
  }, []);

  const clearJobs = useCallback(() => {
    saveJobs([]);
    setJobs([]);
  }, []);

  const callTool = useCallback(
    async (name: string, args: Record<string, unknown>): Promise<CallToolResult> => {
      const current = connectionRef.current;
      if (!current) return { isError: true, content: [{ type: "text", text: "Not connected to the MCP server." }] };
      const result = await current.callTool(name, args);
      if (!result.isError) rememberJobs(extractJobIds(name, resultData(result)));
      return result;
    },
    [rememberJobs],
  );

  const value = useMemo(
    () => ({
      settings,
      updateSettings,
      connection,
      connectionState,
      connectionError,
      tools,
      reconnect: () => setAttempt((n) => n + 1),
      callTool,
      getToken,
      jobs,
      rememberJobs,
      forgetJob,
      clearJobs,
    }),
    [settings, updateSettings, connection, connectionState, connectionError, tools, callTool, getToken, jobs, rememberJobs, forgetJob, clearJobs],
  );
  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp(): AppContextValue {
  const value = useContext(AppContext);
  if (!value) throw new Error("useApp outside AppProvider");
  return value;
}

/** Run `task` every `ms` while mounted, never overlapping (a slow poll just skips a tick). */
export function usePolling(task: () => Promise<void>, ms: number, enabled = true) {
  const taskRef = useRef(task);
  taskRef.current = task;
  useEffect(() => {
    if (!enabled) return;
    let running = false;
    const tick = async () => {
      if (running) return;
      running = true;
      try {
        await taskRef.current();
      } finally {
        running = false;
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), Math.max(250, ms));
    return () => window.clearInterval(id);
  }, [ms, enabled]);
}
