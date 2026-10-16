import { authHeaders, serverEndpoint, type Settings } from "./settings";

export interface Health {
  status: string;
  version: string;
}

export type CellState = "pending" | "running" | "done" | "failed" | string;

export interface JobSummary {
  kind: "render" | "mix" | "singing";
  tool: string;
  job_id: string;
  status: string;
  active: boolean;
  created_at: string | null;
  progress: Record<string, CellState>;
  errors: Record<string, string>;
  error: string | null;
  detail?: string | null;
  source?: string | null;
  voice_id?: string;
}

export interface SessionRow {
  session_id: string;
  client_name: string | null;
  client_version: string | null;
  protocol_version: string | null;
  connected_at: string | null;
  last_seen: string | null;
  requests: number | null;
}

export interface Status extends Health {
  server: { name: string; version: string; mcp_path: string; auth: boolean; output_dir: string };
  jobs: JobSummary[];
  active_jobs: number;
  sessions: { tracked: boolean; stateless: boolean; count: number; clients: SessionRow[] };
}

async function getJson<T>(settings: Settings, path: string): Promise<T> {
  const response = await fetch(serverEndpoint(settings, path), { headers: authHeaders(settings) });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${path}: HTTP ${response.status} ${text.slice(0, 200)}`);
  }
  return (await response.json()) as T;
}

export const getHealth = (settings: Settings) => getJson<Health>(settings, "/healthz");
export const getStatus = (settings: Settings) => getJson<Status>(settings, "/api/status");
