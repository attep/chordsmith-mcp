import { describe, expect, it } from "vitest";
import { prepareToolArguments } from "./arguments";
import type { Tool } from "./mcp";

const tool = {
  name: "create_chord_progression",
  description: "Write chords to MIDI.",
  inputSchema: {
    type: "object",
    properties: {
      chords: { type: "array", items: { type: "string" } },
      filename: { type: "string" },
      tempo: { type: "number" },
      rhythm: {
        anyOf: [{ type: "object", properties: { pattern: { enum: ["block", "pulse"] } } }, { type: "null" }],
      },
    },
    required: ["chords"],
  },
} as unknown as Tool;

describe("prepareToolArguments", () => {
  it("recovers a required list that landed inside another string (the qwen3.6 failure)", () => {
    const raw = '{"filename": "melancholic progression [\\"Am\\", \\"Dm\\", \\"E7\\", \\"Am\\"]"}';
    const prepared = prepareToolArguments(tool, raw);
    expect(prepared.error).toBeUndefined();
    expect(prepared.args.chords).toEqual(["Am", "Dm", "E7", "Am"]);
    expect(prepared.args.filename).toBe("melancholic progression");
    expect(prepared.repaired).toEqual(expect.arrayContaining(["chords", "filename"]));
  });

  it("parses stringified arrays and comma lists", () => {
    expect(prepareToolArguments(tool, { chords: '["Am", "F"]' }).args.chords).toEqual(["Am", "F"]);
    expect(prepareToolArguments(tool, { chords: "Am, F, C, G" }).args.chords).toEqual(["Am", "F", "C", "G"]);
    expect(prepareToolArguments(tool, { chords: "[Am, Dm]" }).args.chords).toEqual(["Am", "Dm"]);
    expect(prepareToolArguments(tool, { chords: "Am" }).args.chords).toEqual(["Am"]);
  });

  it("coerces numbers written as strings and stringified objects", () => {
    const prepared = prepareToolArguments(tool, { chords: ["C"], tempo: "128", rhythm: '{"pattern":"pulse"}' });
    expect(prepared.args.tempo).toBe(128);
    expect(prepared.args.rhythm).toEqual({ pattern: "pulse" });
    expect(prepared.repaired).toContain("tempo");
    expect(prepared.repaired).toContain("rhythm");
  });

  it("leaves well-formed arguments untouched", () => {
    const prepared = prepareToolArguments(tool, { chords: ["Am", "F"], tempo: 100, filename: "song" });
    expect(prepared.error).toBeUndefined();
    expect(prepared.repaired).toEqual([]);
    expect(prepared.args).toEqual({ chords: ["Am", "F"], tempo: 100, filename: "song" });
  });

  it("repairs fenced and trailing-comma JSON", () => {
    expect(prepareToolArguments(tool, '```json\n{"chords": ["C"], "tempo": 120,}\n```').args.chords).toEqual(["C"]);
  });

  it("asks the model to retry when arguments are not JSON", () => {
    const prepared = prepareToolArguments(tool, "please make me a song");
    expect(prepared.error).toMatch(/not valid JSON/);
    expect(prepared.error).toMatch(/create_chord_progression/);
  });

  it("names the missing required field with an example", () => {
    const prepared = prepareToolArguments(tool, { tempo: 100, filename: "song" });
    expect(prepared.error).toBeDefined();
    expect(prepared.error).toMatch(/needs chords/);
    expect(prepared.error).toMatch(/array of strings/);
    expect(prepared.error).toMatch(/sent: tempo, filename/);
  });

  it("tells the model the tool is still available and flags invented fields", () => {
    const prepared = prepareToolArguments(tool, { key: "A minor", rhythm: {}, duration_bars: 8 });
    expect(prepared.error).toBeDefined();
    expect(prepared.error).toMatch(/was NOT executed and is still available/);
    expect(prepared.error).toMatch(/unknown here: key, duration_bars/);
    expect(prepared.error).toMatch(/Accepted fields: chords, filename, tempo, rhythm/);
    expect(prepared.error).toMatch(/Call it again/);
  });

  it("suggests the accepted field for a weak near-miss name", () => {
    const prepared = prepareToolArguments(tool, { filnam: "song", tempo: 100 });
    expect(prepared.error).toMatch(/filnam \(did you mean filename\?\)/);
  });

  it("renames an unambiguous near-miss field and proceeds", () => {
    const prepared = prepareToolArguments(tool, { file: "song", chords: ["Am"] });
    expect(prepared.error).toBeUndefined();
    expect(prepared.args.filename).toBe("song");
    expect(prepared.args.file).toBeUndefined();
    expect(prepared.repaired).toContain("file→filename");
  });

  it("treats 'source' as the input filename (the mix tools' name for it)", () => {
    const prepared = prepareToolArguments(tool, { source: "song.mid", chords: ["Am"] });
    expect(prepared.error).toBeUndefined();
    expect(prepared.args.filename).toBe("song.mid");
    expect(prepared.repaired).toContain("source→filename");
  });
});
