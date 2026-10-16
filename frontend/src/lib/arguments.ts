/** Repair the tool calls small models produce: valid JSON, wrong shape.
 *
 * qwen3.6 and friends like to emit `{"filename": "song [\"Am\", \"Dm\"]"}` (the chord list
 * stuffed into another string), `{"chords": "[\"Am\",\"F\"]"}` (stringified list) or
 * `{"tempo": "120"}` (number as text). The manager has the tool's real JSON Schema, so it can
 * coerce those values back into shape, recover a missing required list from JSON embedded in
 * another string, and — when nothing can be salvaged — hand the model a precise retry message
 * instead of a pydantic dump. The MCP server's own validation stays strict.
 */

import type { Tool } from "./mcp";
import { primaryType, resolve, variants, type JsonSchema } from "./schema";

export interface PreparedArguments {
  args: Record<string, unknown>;
  /** Field names whose values this layer changed (filled, coerced or cleaned). */
  repaired: string[];
  /** Set when the call cannot be salvaged; the Assistant feeds this back to the model. */
  error?: string;
}

function stripFences(text: string): string {
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
  return fenced ? fenced[1].trim() : text;
}

/** The first balanced `open`… `close` run in `text` (string-aware), e.g. a JSON array. */
export function findBalanced(text: string, open: string, close: string): string | null {
  const start = text.indexOf(open);
  if (start === -1) return null;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = start; index < text.length; index++) {
    const char = text[index];
    if (inString) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') inString = false;
      continue;
    }
    if (char === '"') inString = true;
    else if (char === open) depth += 1;
    else if (char === close) {
      depth -= 1;
      if (depth === 0) return text.slice(start, index + 1);
    }
  }
  return null;
}

function stripTrailingCommas(text: string): string {
  return text.replace(/,\s*([}\]])/g, "$1");
}

function parseLooseObject(text: string): Record<string, unknown> | null {
  const candidates = [text, stripFences(text), findBalanced(text, "{", "}")].filter(
    (candidate): candidate is string => Boolean(candidate),
  );
  for (const candidate of candidates) {
    for (const attempt of [candidate.trim(), stripTrailingCommas(candidate.trim())]) {
      try {
        const parsed = JSON.parse(attempt);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          return parsed as Record<string, unknown>;
        }
      } catch {
        // try the next candidate/repair
      }
    }
  }
  return null;
}

/** A list from JSON, a bracket list with bare words (`[Am, Dm]`) or a plain comma list. */
export function parseListText(text: string): unknown[] | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const bracket = trimmed.startsWith("[") ? trimmed : findBalanced(trimmed, "[", "]");
  if (bracket) {
    try {
      const parsed = JSON.parse(bracket);
      if (Array.isArray(parsed)) return parsed;
    } catch {
      const parts = bracket
        .slice(1, -1)
        .split(",")
        .map((part) => part.trim().replace(/^["']|["']$/g, ""))
        .filter(Boolean);
      return parts;
    }
  }
  if (trimmed.includes(",")) {
    return trimmed
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean);
  }
  return [trimmed];
}

interface Coerced {
  value: unknown;
  changed: boolean;
}

/** Coerce one value against its schema: stringified lists/objects, numbers and booleans as text. */
export function coerceValue(value: unknown, schema: JsonSchema, root: JsonSchema, depth = 0): Coerced {
  if (depth > 6 || value === null || value === undefined) return { value, changed: false };
  const resolved = resolve(schema, root);
  const { options } = variants(resolved, root);

  if (typeof value === "string") {
    const text = value.trim();
    for (const option of options) {
      const type = primaryType(option);
      if (type === "array") {
        const parsed = parseListText(text);
        if (parsed) {
          const items = parsed.map((item) => coerceValue(item, option.items ?? {}, root, depth + 1).value);
          return { value: items, changed: true };
        }
      }
      if (type === "object") {
        const parsed = parseLooseObject(text);
        if (parsed) {
          return { value: coerceValue(parsed, option, root, depth + 1).value, changed: true };
        }
      }
      if ((type === "number" || type === "integer") && text !== "" && Number.isFinite(Number(text))) {
        return { value: Number(text), changed: true };
      }
      if (type === "boolean" && /^(true|false)$/i.test(text)) {
        return { value: text.toLowerCase() === "true", changed: true };
      }
    }
    return { value, changed: false };
  }

  if (Array.isArray(value)) {
    const option = options.find((entry) => primaryType(entry) === "array");
    if (!option || primaryType(option) !== "array" || !option.items) return { value, changed: false };
    let changed = false;
    const items = value.map((item) => {
      const result = coerceValue(item, option.items as JsonSchema, root, depth + 1);
      changed = changed || result.changed;
      return result.value;
    });
    return { value: items, changed };
  }

  if (typeof value === "object") {
    const option = options.find((entry) => primaryType(entry) === "object");
    if (!option?.properties) return { value, changed: false };
    let changed = false;
    const out: Record<string, unknown> = {};
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      const childSchema = option.properties[key];
      if (!childSchema) {
        out[key] = child;
        continue;
      }
      const result = coerceValue(child, childSchema, root, depth + 1);
      changed = changed || result.changed;
      out[key] = result.value;
    }
    return { value: out, changed };
  }

  return { value, changed: false };
}

/** Find a `[...]`/`{...}` container embedded in one of the args' string values (top level). */
function harvestContainer(
  args: Record<string, unknown>,
  kind: "array" | "object",
  root: JsonSchema,
  schema: JsonSchema,
): { value: unknown; source: string } | null {
  const [open, close] = kind === "array" ? ["[", "]"] : ["{", "}"];
  for (const [key, value] of Object.entries(args)) {
    if (typeof value !== "string") continue;
    const block = findBalanced(value, open, close);
    if (!block) continue;
    const parsed = kind === "array" ? parseListText(block) : parseLooseObject(block);
    if (!parsed) continue;
    return { value: coerceValue(parsed, schema, root).value, source: key };
  }
  return null;
}

function describeField(name: string, schema: JsonSchema | undefined, root: JsonSchema): string {
  const resolved = resolve(schema ?? {}, root);
  const { options } = variants(resolved, root);
  const option = options[0];
  const type = primaryType(option ?? {});
  if (option?.enum) return `${name} (one of: ${option.enum.map(String).join(", ")})`;
  if (type === "array") {
    const itemType = primaryType(option?.items ? resolve(option.items, root) : {});
    return `${name} (array${itemType ? ` of ${itemType === "string" ? "strings" : itemType}` : ""}, e.g. ["Am", "F", "C", "G"])`;
  }
  return `${name} (${type ?? "value"})`;
}

/**
 * Parse and repair a tool call's arguments against the tool's input schema.
 *
 * Throws nothing; `error` means the call must not be sent — the message is meant for the model.
 */
export function prepareToolArguments(
  tool: Tool,
  raw: string | Record<string, unknown> | undefined | null,
): PreparedArguments {
  let parsed: Record<string, unknown>;
  if (raw === undefined || raw === null) {
    parsed = {};
  } else if (typeof raw === "string") {
    const object = parseLooseObject(stripFences(raw));
    if (!object) {
      return {
        args: {},
        repaired: [],
        error: `${tool.name}: the tool arguments were not valid JSON (${raw.trim().slice(0, 120)}). Send one JSON object with the required fields.`,
      };
    }
    parsed = object;
  } else {
    parsed = raw;
  }

  const root = tool.inputSchema as JsonSchema;
  const resolved = resolve(root, root);
  const properties = resolved.properties ?? {};
  const required = resolved.required ?? [];
  const args: Record<string, unknown> = { ...parsed };
  const repaired: string[] = [];

  for (const [key, value] of Object.entries(args)) {
    const schema = properties[key];
    if (!schema) continue;
    const result = coerceValue(value, schema, root);
    if (result.changed) {
      args[key] = result.value;
      repaired.push(key);
    }
  }

  for (const name of required) {
    if (args[name] !== undefined && args[name] !== null) continue;
    const schema = properties[name];
    if (!schema) continue;
    const type = primaryType(resolve(schema, root));
    if (type !== "array" && type !== "object") continue;
    const found = harvestContainer(args, type, root, schema);
    if (!found) continue;
    args[name] = found.value;
    repaired.push(name);
    // clean the host string so e.g. the filename no longer carries the chord list
    const source = args[found.source];
    if (typeof source === "string") {
      const block = findBalanced(source, type === "array" ? "[" : "{", type === "array" ? "]" : "}");
      if (block) {
        const cleaned = source.replace(block, "").replace(/\s{2,}/g, " ").trim();
        if (cleaned) {
          args[found.source] = cleaned;
          repaired.push(found.source);
        }
      }
    }
  }

  const missing = required.filter((name) => args[name] === undefined || args[name] === null);
  if (missing.length) {
    const sent = Object.keys(parsed).length ? Object.keys(parsed).join(", ") : "(no fields)";
    const fields = missing.map((name) => describeField(name, properties[name], root)).join("; ");
    return {
      args,
      repaired,
      error: `${tool.name} needs ${missing.join(", ")}; this call sent: ${sent}. Send every required field as its own property: ${fields}.`,
    };
  }

  return { args, repaired };
}
