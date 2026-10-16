export interface Settings {
  serverUrl: string;
  mcpPath: string;
  bearerToken: string;
  ollamaUrl: string;
  ollamaModel: string;
  pollMs: number;
}

export const DEFAULT_SETTINGS: Settings = {
  serverUrl: "http://localhost:8000",
  mcpPath: "/mcp",
  bearerToken: "",
  ollamaUrl: "http://localhost:11434",
  ollamaModel: "",
  pollMs: 1500,
};

const KEY = "chordsmith-manager.settings";

export function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return { ...DEFAULT_SETTINGS, ...(JSON.parse(raw) as Partial<Settings>) };
  } catch {
    // corrupt storage: fall back to the defaults
  }
  return { ...DEFAULT_SETTINGS };
}

export function saveSettings(settings: Settings): void {
  localStorage.setItem(KEY, JSON.stringify(settings));
}

export function serverEndpoint(settings: Settings, path: string): string {
  return new URL(path, settings.serverUrl.replace(/\/+$/, "") + "/").toString();
}

export function authHeaders(settings: Settings): Record<string, string> {
  return settings.bearerToken ? { Authorization: `Bearer ${settings.bearerToken}` } : {};
}
