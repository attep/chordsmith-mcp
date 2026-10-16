import { describe, expect, it } from "vitest";
import { extractJobIds, mergeJobs } from "./jobs";

describe("job id extraction", () => {
  it("finds job ids and maps them to their get_* tool", () => {
    const jobs = extractJobIds("render_audio_job", { job_id: "render_abc", status: "queued" });
    expect(jobs).toHaveLength(1);
    expect(jobs[0]).toMatchObject({ job_id: "render_abc", kind: "render", tool: "get_render_job", last_status: "queued" });
    expect(extractJobIds("list_singing_jobs", { jobs: [{ job_id: "job_1" }, { job_id: "job_2" }] }).map((j) => j.tool)).toEqual([
      "get_singing_job",
      "get_singing_job",
    ]);
    expect(extractJobIds("mix_song_with_vocals_job", { job_id: "mix_1" })[0].kind).toBe("mix");
    expect(extractJobIds("create_chord_progression", { filename: "a.mid" })).toEqual([]);
  });

  it("merges updates without losing the original record", () => {
    const [first] = extractJobIds("render_audio_job", { job_id: "render_1", status: "queued" });
    const merged = mergeJobs([first], [{ ...first, source_tool: "dashboard", last_status: "done" }]);
    expect(merged).toHaveLength(1);
    expect(merged[0]).toMatchObject({ source_tool: "render_audio_job", last_status: "done" });
  });
});
