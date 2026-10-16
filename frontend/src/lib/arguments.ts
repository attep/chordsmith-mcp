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

function levenshtein(a: string, b: string): number {
  const rows = a.length + 1;
  const cols = b.length + 1;
  let previous = Array.from({ length: cols }, (_, index) => index);
  for (let row = 1; row < rows; row++) {
    const current = [row];
    for (let col = 1; col < cols; col++) {
      const cost = a[row - 1] === b[col - 1] ? 0 : 1;
      current[col] = Math.min(current[col - 1] + 1, previous[col] + 1, previous[col - 1] + cost);
    }
    previous = current;
  }
  return previous[cols - 1];
}

/** Lower is closer: 0 exact, 1 prefix either way, 2 contains, 3+levenshtein. */
function nameScore(name: string, candidate: string): number {
  const needle = name.toLowerCase();
  const hay = candidate.toLowerCase();
  if (hay === needle) return 0;
  if (hay.startsWith(needle) || needle.startsWith(hay)) return 1;
  if (hay.includes(needle)) return 2;
  return 3 + levenshtein(needle, hay);
}

/** The accepted property an invented name probably meant ('file' -> 'filename'). */
export function closestName(name: string, candidates: string[]): string | undefined {
  let best: string | undefined;
  let bestScore = Infinity;
  for (const candidate of candidates) {
    const score = nameScore(name, candidate);
    if (score < bestScore) {
      bestScore = score;
      best = candidate;
    }
  }
  return bestScore <= 5 ? best : undefined;
}

/** Strong enough to rename silently: a prefix/contains match or one typo away (levenshtein ≤ 1). */
function isStrongMatch(score: number): boolean {
  return score <= 4;
}

function describeField(name: string, schema: JsonSchema | undefined, root: JsonSchema): string {
  const { options } = variants(resolve(schema ?? {}, root), root);
  if (!options.length) return `${name} (value)`;
  const described = options.map((option) => {
    if (option.enum) return `one of: ${option.enum.map(String).join(", ")}`;
    const type = primaryType(option);
    if (type === "array") {
      const itemType = primaryType(option.items ? resolve(option.items, root) : {});
      const items =
        itemType === "string" ? " of strings" : itemType === "object" ? " of objects" : itemType ? ` of ${itemType}s` : "";
      return `array${items}`;
    }
    if (type === "integer") return "number";
    return type ?? "value";
  });
  const unique = [...new Set(described)];
  const example = unique.some((entry) => entry.startsWith("array")) ? ', e.g. ["Am", "F", "C", "G"]' : "";
  return `${name} (${unique.join(" or ")}${example})`;
}

/** Common ways models name the input file; "filename" is that property in most tools. */
const FILENAME_ALIASES = new Set(["file", "source", "path", "file_name", "filename", "midi_file"]);

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
  const args: Record<string, unknown> = {};
  const repaired: string[] = [];

  // unambiguous near-miss keys are renamed first ('file'/'source' -> 'filename'), so small-model
  // calls that are otherwise complete just work; weak matches stay unknown and get a suggestion
  for (const [key, value] of Object.entries(parsed)) {
    if (key in properties) {
      args[key] = value;
      continue;
    }
    const lower = key.toLowerCase();
    const alias = "filename" in properties && lower !== "filename" && FILENAME_ALIASES.has(lower) ? "filename" : undefined;
    const guess = alias ?? closestName(key, Object.keys(properties));
    if (guess && guess !== key && (alias !== undefined || isStrongMatch(nameScore(key, guess))) && !(guess in parsed)) {
      args[guess] = value;
      repaired.push(`${key}→${guess}`);
    } else {
      args[key] = value;
    }
  }

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
    // unions count too: "chords: str | list" can hold the list the model hid elsewhere
    const { options } = variants(resolve(schema, root), root);
    const kind = (["array", "object"] as const).find((candidate) =>
      options.some((option) => primaryType(option) === candidate),
    );
    if (!kind) continue;
    const found = harvestContainer(args, kind, root, schema);
    if (!found) continue;
    args[name] = found.value;
    repaired.push(name);
    // clean the host string so e.g. the filename no longer carries the chord list
    const source = args[found.source];
    if (typeof source === "string") {
      const block = findBalanced(source, kind === "array" ? "[" : "{", kind === "array" ? "]" : "}");
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
    const sentKeys = Object.keys(parsed);
    const sent = sentKeys.length ? sentKeys.join(", ") : "(no fields)";
    const acceptedNames = Object.keys(properties);
    const unknown = Object.keys(args)
      .filter((key) => !(key in properties))
      .map((key) => {
        const guess = closestName(key, acceptedNames);
        return guess && guess !== key ? `${key} (did you mean ${guess}?)` : key;
      });
    const fields = missing.map((name) => describeField(name, properties[name], root)).join("; ");
    const accepted = acceptedNames.slice(0, 12).join(", ") + (acceptedNames.length > 12 ? ", …" : "");
    return {
      args,
      repaired,
      error: [
        `${tool.name} was NOT executed and is still available — it needs ${missing.join(", ")}.`,
        `This call sent: ${sent}${unknown.length ? ` (unknown here: ${unknown.join(", ")})` : ""}.`,
        `Required: ${fields}.`,
        `Accepted fields: ${accepted}.`,
        "Call it again with each field as its own separate property.",
      ].join(" "),
    };
  }

  return { args, repaired };
}
