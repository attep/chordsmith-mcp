export type JobKind = "render" | "mix" | "singing";

export interface KnownJob {
  job_id: string;
  kind: JobKind;
  tool: string;
  source_tool: string;
  saved_at: number;
  last_status?: string;
}

const KEY = "chordsmith-manager.jobs";
const MAX_JOBS = 200;

export const JOB_TOOLS: Record<JobKind, string> = {
  render: "get_render_job",
  mix: "get_mix_job",
  singing: "get_singing_job",
};

export function jobKind(jobId: string, toolName = ""): JobKind | null {
  if (jobId.startsWith("render_") || toolName.startsWith("render_audio")) return "render";
  if (jobId.startsWith("mix_") || toolName.startsWith("mix_")) return "mix";
  if (jobId.startsWith("job_") || toolName.includes("singing")) return "singing";
  return null;
}

export function loadJobs(): KnownJob[] {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as KnownJob[]) : [];
  } catch {
    return [];
  }
}

export function saveJobs(jobs: KnownJob[]): void {
  localStorage.setItem(KEY, JSON.stringify(jobs.slice(0, MAX_JOBS)));
}

/** Find job ids in a tool result (structuredContent or JSON text), e.g. from render_audio_job. */
export function extractJobIds(toolName: string, value: unknown): KnownJob[] {
  const found = new Map<string, KnownJob>();
  const visit = (node: unknown, depth: number) => {
    if (depth > 4 || node === null || typeof node !== "object") return;
    if (Array.isArray(node)) {
      node.forEach((item) => visit(item, depth + 1));
      return;
    }
    const record = node as Record<string, unknown>;
    const id = record.job_id;
    if (typeof id === "string") {
      const kind = jobKind(id, toolName);
      if (kind && !found.has(id)) {
        found.set(id, {
          job_id: id,
          kind,
          tool: JOB_TOOLS[kind],
          source_tool: toolName,
          saved_at: Date.now(),
          last_status: typeof record.status === "string" ? record.status : undefined,
        });
      }
    }
    Object.values(record).forEach((child) => visit(child, depth + 1));
  };
  visit(value, 0);
  return [...found.values()];
}

export function mergeJobs(existing: KnownJob[], incoming: KnownJob[]): KnownJob[] {
  const byId = new Map(existing.map((job) => [job.job_id, job]));
  for (const job of incoming) {
    const previous = byId.get(job.job_id);
    byId.set(job.job_id, previous ? { ...previous, last_status: job.last_status ?? previous.last_status } : job);
  }
  return [...byId.values()].sort((a, b) => b.saved_at - a.saved_at);
}

export const isFinal = (status?: string) => status === "done" || status === "failed";
