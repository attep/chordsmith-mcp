import { useMemo, useState } from "react";
import { getHealth, getSession, getStatus, type Health, type SessionDetail, type Status } from "../lib/api";
import { useApp, usePolling } from "../lib/context";
import { isFinal, JOB_TOOLS, type JobKind } from "../lib/jobs";
import { resultData } from "../lib/mcp";
import { ProgressMatrix } from "./ProgressMatrix";

export interface JobRow {
  job_id: string;
  kind: JobKind;
  tool: string;
  status: string;
  created_at: string | null;
  progress: Record<string, string>;
  errors: Record<string, string>;
  error: string | null;
  detail?: string | null;
}

interface ToolDetail {
  status: string;
  progress?: Record<string, string>;
  errors?: Record<string, string>;
  error?: string | null;
  detail?: string | null;
}

/** get_render_job/get_mix_job return progress as [{track, state, error}]; get_singing_job as text. */
function parseDetail(kind: JobKind, data: unknown): ToolDetail | null {
  if (!data || typeof data !== "object") return null;
  const record = data as Record<string, unknown>;
  if (typeof record.status !== "string") return null;
  const detail: ToolDetail = { status: record.status, error: (record.error as string | null) ?? null };
  if (Array.isArray(record.progress)) {
    detail.progress = {};
    detail.errors = {};
    for (const cell of record.progress as { track: string; state: string; error?: string | null }[]) {
      detail.progress[cell.track] = cell.state;
      if (cell.error) detail.errors[cell.track] = cell.error;
    }
  } else if (kind === "singing") {
    detail.detail = typeof record.progress === "string" ? record.progress : null;
  }
  return detail;
}

export function Dashboard({ onOpenTool }: { onOpenTool: (name: string, args: Record<string, unknown>) => void }) {
  const { settings, connection, connectionState, getToken, jobs, rememberJobs } = useApp();
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [details, setDetails] = useState<Record<string, ToolDetail>>({});
  const [showFinished, setShowFinished] = useState(true);
  const [lastPoll, setLastPoll] = useState<Date | null>(null);
  const [selectedSession, setSelectedSession] = useState<string | null>(null);
  const [sessionDetail, setSessionDetail] = useState<SessionDetail | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);

  usePolling(
    async () => {
      try {
        setHealth(await getHealth(settings, await getToken()));
        setHealthError(null);
      } catch (error) {
        setHealth(null);
        setHealthError(String(error instanceof Error ? error.message : error));
      }
    },
    5000,
  );

  usePolling(async () => {
    let current: Status | null = null;
    try {
      current = await getStatus(settings, await getToken());
      setStatus(current);
      setStatusError(null);
    } catch (error) {
      setStatusError(String(error instanceof Error ? error.message : error));
    }
    if (!connection) return;
    // Poll the get_*_job tools for every job that is not finished (server-side or saved locally).
    const watch = new Map<string, JobKind>();
    for (const job of current?.jobs ?? []) if (job.active) watch.set(job.job_id, job.kind);
    for (const job of jobs) if (!isFinal(job.last_status) && job.last_status !== "expired") watch.set(job.job_id, job.kind);
    const updates = await Promise.all(
      [...watch].map(async ([jobId, kind]) => {
        const result = await connection.callTool(JOB_TOOLS[kind], { job_id: jobId });
        if (result.isError) return [jobId, kind, null] as const;
        return [jobId, kind, parseDetail(kind, resultData(result))] as const;
      }),
    );
    setDetails((old) => {
      const next = { ...old };
      for (const [jobId, , detail] of updates) if (detail) next[jobId] = detail;
      return next;
    });
    rememberJobs(
      updates.map(([jobId, kind, detail]) => ({
        job_id: jobId,
        kind,
        tool: JOB_TOOLS[kind],
        source_tool: "dashboard",
        saved_at: Date.now(),
        last_status: detail ? detail.status : "expired",
      })),
    );
    setLastPoll(new Date());
  }, settings.pollMs);

  usePolling(
    async () => {
      if (!selectedSession) return;
      try {
        setSessionDetail(await getSession(settings, selectedSession, await getToken()));
        setSessionError(null);
      } catch (error) {
        setSessionDetail(null);
        setSessionError(String(error instanceof Error ? error.message : error));
      }
    },
    settings.pollMs,
    Boolean(selectedSession),
  );

  const rows = useMemo<JobRow[]>(() => {
    const byId = new Map<string, JobRow>();
    for (const job of status?.jobs ?? []) {
      byId.set(job.job_id, {
        job_id: job.job_id,
        kind: job.kind,
        tool: job.tool,
        status: job.status,
        created_at: job.created_at,
        progress: job.progress,
        errors: job.errors,
        error: job.error,
        detail: job.detail,
      });
    }
    for (const job of jobs) {
      if (!byId.has(job.job_id)) {
        byId.set(job.job_id, {
          job_id: job.job_id,
          kind: job.kind,
          tool: job.tool,
          status: job.last_status ?? "unknown",
          created_at: new Date(job.saved_at).toISOString(),
          progress: {},
          errors: {},
          error: null,
        });
      }
    }
    for (const [jobId, detail] of Object.entries(details)) {
      const row = byId.get(jobId);
      if (!row) continue;
      row.status = detail.status;
      if (detail.progress && Object.keys(detail.progress).length) row.progress = detail.progress;
      if (detail.errors) row.errors = { ...row.errors, ...detail.errors };
      if (detail.error !== undefined) row.error = detail.error;
      if (detail.detail !== undefined) row.detail = detail.detail;
    }
    const all = [...byId.values()].sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""));
    return showFinished ? all : all.filter((row) => !isFinal(row.status) && row.status !== "expired");
  }, [status, jobs, details, showFinished]);

  const active = rows.filter((row) => row.status === "queued" || row.status === "running").length;
  const sessions = status?.sessions;

  return (
    <div className="dashboard">
      <div className="cards">
        <div className={`card ${health ? "ok" : "bad"}`}>
          <h3>Health</h3>
          {health ? (
            <>
              <div className="big">{health.status}</div>
              <div>version {health.version}</div>
            </>
          ) : (
            <div className="error-text">{healthError ?? "checking…"}</div>
          )}
          <div className="muted small-text">GET {settings.serverUrl}/healthz</div>
        </div>
        <div className={`card ${connectionState === "connected" ? "ok" : "bad"}`}>
          <h3>MCP session</h3>
          <div className="big">{connectionState}</div>
          {connection && (
            <>
              <div>
                {connection.serverVersion?.name} {connection.serverVersion?.version}
              </div>
              <div className="muted small-text">session {connection.sessionId ?? "(stateless)"}</div>
            </>
          )}
        </div>
        <div className="card">
          <h3>Jobs</h3>
          <div className="big">{active} active</div>
          <div>{rows.length} shown</div>
          <div className="muted small-text">{lastPoll ? `polled ${lastPoll.toLocaleTimeString()}` : "…"}</div>
        </div>
        <div className="card">
          <h3>Clients</h3>
          <div className="big">{sessions?.count ?? "–"}</div>
          <div className="muted small-text">{status ? status.server.output_dir : statusError ?? "…"}</div>
        </div>
      </div>

      {statusError && <div className="banner error">/api/status: {statusError}</div>}

      <section>
        <h2>
          Jobs
          <label className="inline">
            <input type="checkbox" checked={showFinished} onChange={(e) => setShowFinished(e.target.checked)} /> show finished
          </label>
        </h2>
        <ProgressMatrix rows={rows} onOpen={(row) => onOpenTool(row.tool, { job_id: row.job_id })} />
      </section>

      <section>
        <h2>Connected clients</h2>
        {sessions && !sessions.tracked && (
          <p className="muted">
            {sessions.stateless ? "The server runs stateless, so sessions are not tracked." : "Session tracking is off."}
          </p>
        )}
        <table className="table">
          <thead>
            <tr>
              <th>Client</th>
              <th>Session</th>
              <th>Protocol</th>
              <th>Connected</th>
              <th>Last seen</th>
              <th>Requests</th>
            </tr>
          </thead>
          <tbody>
            {(sessions?.clients ?? []).map((client) => (
              <tr
                key={client.session_id}
                className={[
                  client.session_id === connection?.sessionId ? "self" : "",
                  "clickable",
                  client.session_id === selectedSession ? "selected" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                onClick={() =>
                  setSelectedSession((current) => (current === client.session_id ? null : client.session_id))
                }
                title="Click to see what this client is doing"
              >
                <td>
                  {client.client_name ?? "unknown"} {client.client_version}
                  {client.session_id === connection?.sessionId && <span className="badge">this tab</span>}
                </td>
                <td>
                  <code>{client.session_id.slice(0, 12)}…</code>
                </td>
                <td>{client.protocol_version ?? "–"}</td>
                <td>{client.connected_at ? new Date(client.connected_at).toLocaleTimeString() : "–"}</td>
                <td>{client.last_seen ? new Date(client.last_seen).toLocaleTimeString() : "–"}</td>
                <td>{client.requests ?? "–"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {selectedSession && (
        <section className="session-detail">
          <h2>
            Client activity — {sessionDetail?.client_name ?? "unknown"}{" "}
            {sessionDetail?.client_version ?? ""}
            <button type="button" className="small" onClick={() => {
              setSelectedSession(null);
              setSessionDetail(null);
              setSessionError(null);
            }}>
              close
            </button>
          </h2>
          <p className="muted small-text">
            session <code>{selectedSession}</code>
            {sessionDetail?.requests != null ? ` · ${sessionDetail.requests} requests` : ""}
            {sessionDetail?.last_seen ? ` · last seen ${new Date(sessionDetail.last_seen).toLocaleTimeString()}` : ""}
          </p>
          {sessionError && <div className="banner error">{sessionError}</div>}
          {sessionDetail && sessionDetail.activity.length === 0 && (
            <p className="muted">No activity recorded yet.</p>
          )}
          {sessionDetail && sessionDetail.activity.length > 0 && (
            <table className="table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Request</th>
                  <th>Details</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {[...sessionDetail.activity].reverse().map((entry, index) => (
                  <tr key={`${entry.at}-${index}`}>
                    <td>{new Date(entry.at).toLocaleTimeString()}</td>
                    <td>
                      <code>
                        {entry.method}
                        {entry.tool ? ` → ${entry.tool}` : ""}
                      </code>
                    </td>
                    <td>
                      <code className="activity-args">{entry.arguments ?? ""}</code>
                    </td>
                    <td>{entry.status ?? "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}
    </div>
  );
}
