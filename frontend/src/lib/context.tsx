import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { extractJobIds, loadJobs, mergeJobs, saveJobs, type KnownJob } from "./jobs";
import { McpConnection, resultData, type CallToolResult, type Tool } from "./mcp";
import { loadSettings, saveSettings, type Settings } from "./settings";

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

  useEffect(() => {
    let cancelled = false;
    setConnectionState("connecting");
    setConnectionError(null);
    McpConnection.open(settings)
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
        setConnectionError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
      const current = connectionRef.current;
      connectionRef.current = null;
      if (current) void current.close();
    };
  }, [settings.serverUrl, settings.mcpPath, settings.bearerToken, attempt]); // eslint-disable-line react-hooks/exhaustive-deps

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
      jobs,
      rememberJobs,
      forgetJob,
      clearJobs,
    }),
    [settings, updateSettings, connection, connectionState, connectionError, tools, callTool, jobs, rememberJobs, forgetJob, clearJobs],
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
