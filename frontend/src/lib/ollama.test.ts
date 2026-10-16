import { describe, expect, it } from "vitest";
import { compactForModel, parseArguments, toOllamaTool } from "./ollama";
import type { Tool } from "./mcp";

const tool = {
  name: "create_chord_progression",
  description: "Write chords to MIDI.",
  inputSchema: {
    type: "object",
    $defs: {
      ChordEvent: { type: "object", properties: { chord: { type: "string" }, beats: { type: "number" } }, required: ["chord"] },
      Rhythm: { type: "object", properties: { pattern: { enum: ["block", "pulse"], default: "block" } } },
    },
    properties: {
      chords: { type: "array", items: { anyOf: [{ type: "string" }, { $ref: "#/$defs/ChordEvent" }] } },
      preset: { anyOf: [{ const: "lofi", type: "string" }, { type: "null" }], default: null },
      rhythm: { anyOf: [{ $ref: "#/$defs/Rhythm" }, { type: "null" }], default: null },
      tempo: { type: "number", default: 120, minimum: 20 },
    },
    required: ["chords"],
  },
} as unknown as Tool;

describe("toOllamaTool", () => {
  const converted = toOllamaTool(tool);
  const props = converted.function.parameters.properties!;

  it("keeps name, description and required", () => {
    expect(converted.type).toBe("function");
    expect(converted.function.name).toBe("create_chord_progression");
    expect(converted.function.parameters.required).toEqual(["chords"]);
  });

  it("inlines refs and drops null alternatives", () => {
    expect(props.rhythm.type).toBe("object");
    expect(props.rhythm.properties!.pattern.enum).toEqual(["block", "pulse"]);
    expect(props.preset).toMatchObject({ type: "string", enum: ["lofi"] });
    expect(JSON.stringify(converted)).not.toContain("$ref");
  });

  it("represents mixed unions as a type list with the rich structure", () => {
    expect(props.chords.items!.type).toEqual(["string", "object"]);
    expect(props.chords.items!.properties!.chord.type).toBe("string");
  });

  it("moves defaults into the description", () => {
    expect(props.tempo.description).toBe("Default: 120.");
    expect(props.tempo.minimum).toBe(20);
  });
});

describe("tool results for the model", () => {
  it("omits base64 payloads and truncates", () => {
    const text = compactForModel({ filename: "a.mid", data_base64: "QUJD" });
    expect(text).toContain("<4 characters omitted>");
    expect(compactForModel({ x: "y".repeat(100) }, 20)).toMatch(/truncated\)$/);
  });

  it("parses string or object arguments", () => {
    expect(parseArguments('{"a":1}')).toEqual({ a: 1 });
    expect(parseArguments({ b: 2 })).toEqual({ b: 2 });
    expect(parseArguments("not json")).toEqual({});
  });
});
