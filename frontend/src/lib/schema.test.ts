import { describe, expect, it } from "vitest";
import { initialValue, matchVariant, prune, resolve, variants, type JsonSchema } from "./schema";

const root: JsonSchema = {
  type: "object",
  $defs: {
    Voicing: {
      type: "object",
      properties: { style: { enum: ["close", "open", "drop2"], default: "close" }, voice_leading: { type: "boolean" } },
    },
    ChordEvent: { type: "object", properties: { chord: { type: "string" }, beats: { type: "number" } }, required: ["chord"] },
  },
  properties: {
    chords: { type: "array", items: { anyOf: [{ type: "string" }, { $ref: "#/$defs/ChordEvent" }] } },
    tempo: { type: "number", default: 120 },
    voicing: { anyOf: [{ $ref: "#/$defs/Voicing" }, { type: "null" }], default: null, description: "How chords are arranged" },
  },
  required: ["chords"],
};

describe("schema helpers", () => {
  it("resolves refs and keeps sibling keywords", () => {
    const voicing = variants(resolve(root.properties!.voicing, root), root);
    expect(voicing.nullable).toBe(true);
    expect(voicing.options).toHaveLength(1);
    expect(voicing.options[0].properties?.style.enum).toEqual(["close", "open", "drop2"]);
  });

  it("builds initial values from defaults and required keys", () => {
    expect(initialValue(root, root)).toEqual({ chords: [] });
    expect(initialValue({ $ref: "#/$defs/ChordEvent" }, root)).toEqual({ chord: "" });
    expect(initialValue(root.properties!.tempo, root)).toBe(120);
  });

  it("matches anyOf variants by value type", () => {
    const options = variants(root.properties!.chords.items!, root).options;
    expect(matchVariant("Am", options)).toBe(0);
    expect(matchVariant({ chord: "G7", beats: 2 }, options)).toBe(1);
  });

  it("prunes omitted fields", () => {
    expect(prune({ a: 1, b: undefined, c: { d: undefined, e: [1, { f: undefined }] } })).toEqual({ a: 1, c: { e: [1, {}] } });
  });
});
