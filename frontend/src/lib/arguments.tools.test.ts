/**
 * Sweep every ChordSmith tool schema: the repair layer must turn "small model style" arguments
 * (lists as comma strings or stringified JSON, numbers as text, a required list embedded in
 * another string) into values the server accepts — so one failing call pattern never needs
 * hand-debugging again. Schemas come from the generated fixture (descriptions stripped).
 */

import { describe, expect, it } from "vitest";
import { prepareToolArguments } from "./arguments";
import { primaryType, resolve, variants, type JsonSchema } from "./schema";
import { TOOL_SCHEMAS, type ToolSchemasFixture } from "./toolSchemas.fixture";
import type { Tool } from "./mcp";

const asTool = (fixture: ToolSchemasFixture): Tool =>
  ({ name: fixture.name, inputSchema: fixture.inputSchema }) as unknown as Tool;

function optionFor(schema: JsonSchema, root: JsonSchema): JsonSchema {
  const resolved = resolve(schema, root);
  const { options } = variants(resolved, root);
  const nonNull = options.filter((option) => primaryType(option) !== "null");
  // prefer the rich alternative (an array/object structure) so unions like string|ChordEvent
  // exercise the structured path
  return nonNull.find((option) => option.items || option.properties) ?? nonNull[0] ?? resolved;
}

/** A correctly shaped sample for a schema (first enum value, minimum numbers, 2-item lists). */
function sampleValue(schema: JsonSchema, root: JsonSchema, depth = 0): unknown {
  if (depth > 5) return "example";
  const option = optionFor(schema, root);
  if (option.enum) return option.enum[0];
  if (option.const !== undefined) return option.const;
  switch (primaryType(option)) {
    case "string":
      return "example";
    case "number":
    case "integer":
      return option.minimum ?? 1;
    case "boolean":
      return true;
    case "array": {
      const items = (option.items ?? {}) as JsonSchema;
      return [sampleValue(items, root, depth + 1), sampleValue(items, root, depth + 1)];
    }
    case "object": {
      const properties = option.properties ?? {};
      const out: Record<string, unknown> = {};
      for (const key of Object.keys(properties).slice(0, 4)) {
        out[key] = sampleValue(properties[key], root, depth + 1);
      }
      return out;
    }
    default:
      return "example";
  }
}

/** Closest kind of a type per the resolved schema (for assertions). */
function kindOf(schema: JsonSchema, root: JsonSchema): string | undefined {
  return primaryType(optionFor(schema, root));
}

/** "Small model style": lists and objects become strings, numbers/booleans become text. */
function malformedValue(value: unknown, stringifyArrays: boolean): unknown {
  if (Array.isArray(value)) {
    if (value.every((item) => typeof item === "string") && !stringifyArrays) {
      return value.join(", ");
    }
    return JSON.stringify(value);
  }
  if (value && typeof value === "object") return JSON.stringify(value);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return value;
}

function requiredOf(fixture: ToolSchemasFixture): { properties: Record<string, JsonSchema>; required: string[] } {
  const root = fixture.inputSchema as JsonSchema;
  const resolved = resolve(root, root);
  return { properties: (resolved.properties ?? {}) as Record<string, JsonSchema>, required: resolved.required ?? [] };
}

describe("every tool schema accepts repaired small-model arguments", () => {
  for (const fixture of TOOL_SCHEMAS) {
    const { properties, required } = requiredOf(fixture);

    it(`${fixture.name}: comma/stringified values are repaired to the schema shapes`, () => {
      for (const stringifyArrays of [false, true]) {
        const args: Record<string, unknown> = {};
        for (const name of required) {
          args[name] = malformedValue(sampleValue(properties[name], fixture.inputSchema as JsonSchema), stringifyArrays);
        }
        const prepared = prepareToolArguments(asTool(fixture), args);
        expect(prepared.error, `${fixture.name} (${stringifyArrays}) -> ${prepared.error}`).toBeUndefined();
        for (const name of required) {
          const kind = kindOf(properties[name], fixture.inputSchema as JsonSchema);
          const value = prepared.args[name];
          if (kind === "array") expect(Array.isArray(value), `${fixture.name}.${name} should be an array`).toBe(true);
          else if (kind === "number" || kind === "integer") expect(typeof value).toBe("number");
          else if (kind === "boolean") expect(typeof value).toBe("boolean");
          else if (kind === "object") expect(typeof value).toBe("object");
          else expect(typeof value).toBe("string");
        }
      }
    });

    if (required.length > 0) {
      it(`${fixture.name}: an empty call gets the still-available retry message`, () => {
        const prepared = prepareToolArguments(asTool(fixture), {});
        expect(prepared.error).toMatch(/was NOT executed and is still available/);
        for (const name of required) expect(prepared.error).toContain(name);
      });
    }

    if (required.length >= 1) {
      it(`${fixture.name}: a required list/object hidden in another string is recovered`, () => {
        const root = fixture.inputSchema as JsonSchema;
        const valid: Record<string, unknown> = {};
        for (const name of required) valid[name] = sampleValue(properties[name], root);
        const container = required.find((name) => ["array", "object"].includes(kindOf(properties[name], root) ?? ""));
        if (!container) return; // nothing container-shaped to hide
        const host = Object.keys(properties).find(
          (name) => name !== container && kindOf(properties[name], root) === "string",
        );
        if (!host) return; // no string field to hide it in
        const args: Record<string, unknown> = { ...valid };
        delete args[container];
        args[host] = `please use ${JSON.stringify(valid[container])}`;
        const prepared = prepareToolArguments(asTool(fixture), args);
        expect(prepared.error, `${fixture.name} -> ${prepared.error}`).toBeUndefined();
        expect(prepared.repaired).toContain(container);
        expect(prepared.args[host]).toBe("please use");
      });
    }
  }

  it("covers every registered tool", () => {
    expect(TOOL_SCHEMAS.length).toBeGreaterThanOrEqual(45);
  });
});
