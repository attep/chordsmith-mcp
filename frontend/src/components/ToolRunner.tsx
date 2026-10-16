import { useEffect, useMemo, useState } from "react";
import { useApp } from "../lib/context";
import { isFinal } from "../lib/jobs";
import type { CallToolResult, Tool } from "../lib/mcp";
import { initialValue, prune, type JsonSchema } from "../lib/schema";
import { ToolResultView } from "./JsonView";
import { SchemaForm } from "./SchemaForm";

interface Run {
  tool: string;
  args: Record<string, unknown>;
  result: CallToolResult;
  ms: number;
}

const startArgs = (tool: Tool) =>
  (initialValue(tool.inputSchema as JsonSchema, tool.inputSchema as JsonSchema) ?? {}) as Record<string, unknown>;

export function ToolRunner({ request }: { request: { name: string; args: Record<string, unknown> } | null }) {
  const { tools, callTool, jobs, forgetJob, clearJobs } = useApp();
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [args, setArgs] = useState<Record<string, unknown>>({});
  const [mode, setMode] = useState<"form" | "json">("form");
  const [jsonText, setJsonText] = useState("{}");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [run, setRun] = useState<Run | null>(null);

  const tool = tools.find((entry) => entry.name === selected) ?? null;
  const visible = useMemo(() => {
    const needle = filter.toLowerCase();
    return tools.filter(
      (entry) => entry.name.toLowerCase().includes(needle) || (entry.title ?? "").toLowerCase().includes(needle),
    );
  }, [tools, filter]);

  const select = (name: string, preset?: Record<string, unknown>) => {
    const next = tools.find((entry) => entry.name === name);
    if (!next) return;
    setSelected(name);
    const value = preset ?? startArgs(next);
    setArgs(value);
    setJsonText(JSON.stringify(value, null, 2));
    setJsonError(null);
    setRun(null);
  };

  useEffect(() => {
    if (request && tools.length) select(request.name, request.args);
  }, [request, tools]); // eslint-disable-line react-hooks/exhaustive-deps

  const switchMode = (next: "form" | "json") => {
    if (next === "json") setJsonText(JSON.stringify(prune(args), null, 2));
    if (next === "form" && !jsonError) {
      try {
        setArgs(JSON.parse(jsonText || "{}"));
      } catch {
        return;
      }
    }
    setMode(next);
  };

  const submit = async (overrideTool?: string, overrideArgs?: Record<string, unknown>) => {
    const name = overrideTool ?? tool?.name;
    if (!name) return;
    let payload = overrideArgs;
    if (!payload) {
      if (mode === "json") {
        try {
          payload = JSON.parse(jsonText || "{}") as Record<string, unknown>;
        } catch (error) {
          setJsonError(String(error));
          return;
        }
      } else {
        payload = prune(args) as Record<string, unknown>;
      }
    }
    setBusy(true);
    const started = performance.now();
    const result = await callTool(name, payload);
    setRun({ tool: name, args: payload, result, ms: Math.round(performance.now() - started) });
    setBusy(false);
  };

  const annotations = tool?.annotations;
  return (
    <div className="tool-runner">
      <aside className="tool-list">
        <input placeholder={`Filter ${tools.length} tools…`} value={filter} onChange={(e) => setFilter(e.target.value)} />
        <ul>
          {visible.map((entry) => (
            <li key={entry.name}>
              <button className={entry.name === selected ? "tool-item active" : "tool-item"} onClick={() => select(entry.name)}>
                <span className="tool-name">{entry.name}</span>
                {entry.annotations?.readOnlyHint && <span className="badge ro">read-only</span>}
                {entry.annotations?.destructiveHint && <span className="badge danger">destructive</span>}
              </button>
            </li>
          ))}
        </ul>
        <section className="recent-jobs">
          <h3>
            Saved jobs ({jobs.length})
            {jobs.length > 0 && (
              <button className="small" onClick={clearJobs}>
                clear
              </button>
            )}
          </h3>
          {jobs.length === 0 && <p className="muted">Job ids from tool results are saved here (localStorage).</p>}
          <ul>
            {jobs.slice(0, 30).map((job) => (
              <li key={job.job_id} className="job-row">
                <button className="link" title={`from ${job.source_tool}`} onClick={() => select(job.tool, { job_id: job.job_id })}>
                  {job.job_id}
                </button>
                <span className={`state state-${isFinal(job.last_status) ? job.last_status : "running"}`}>
                  {job.last_status ?? "?"}
                </span>
                <button className="small" onClick={() => forgetJob(job.job_id)}>
                  ✕
                </button>
              </li>
            ))}
          </ul>
        </section>
      </aside>
      <section className="tool-detail">
        {!tool && <p className="muted">Pick a tool on the left. Forms are generated from each tool's inputSchema.</p>}
        {tool && (
          <>
            <h2>
              {tool.title ?? tool.name} <code>{tool.name}</code>
            </h2>
            <div className="badges">
              {annotations?.readOnlyHint && <span className="badge ro">read-only</span>}
              {annotations?.destructiveHint && <span className="badge danger">destructive</span>}
              {annotations?.idempotentHint && <span className="badge">idempotent</span>}
            </div>
            <details className="description">
              <summary>Description</summary>
              <p className="pre-wrap">{tool.description}</p>
            </details>
            <div className="mode-switch">
              <button className={mode === "form" ? "tab active" : "tab"} onClick={() => switchMode("form")}>
                Form
              </button>
              <button className={mode === "json" ? "tab active" : "tab"} onClick={() => switchMode("json")}>
                JSON
              </button>
              <button className="small" onClick={() => select(tool.name)}>
                reset
              </button>
            </div>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void submit();
              }}
            >
              {mode === "form" ? (
                <SchemaForm schema={tool.inputSchema as JsonSchema} value={args} onChange={setArgs} />
              ) : (
                <>
                  <textarea
                    className={jsonError ? "json-editor invalid" : "json-editor"}
                    rows={14}
                    value={jsonText}
                    onChange={(event) => {
                      setJsonText(event.target.value);
                      try {
                        JSON.parse(event.target.value || "{}");
                        setJsonError(null);
                      } catch (error) {
                        setJsonError(String(error));
                      }
                    }}
                  />
                  {jsonError && <p className="error-text">{jsonError}</p>}
                </>
              )}
              <div className="row">
                <button type="submit" className="primary" disabled={busy}>
                  {busy ? "Calling…" : "Call tool"}
                </button>
              </div>
            </form>
            {run && (
              <div className="run">
                <h3>
                  Result <span className="muted">({run.ms} ms)</span>
                </h3>
                <ToolResultView result={run.result} maxHeight={520} />
                <details>
                  <summary>Arguments sent</summary>
                  <pre className="json">{JSON.stringify(run.args, null, 2)}</pre>
                </details>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
