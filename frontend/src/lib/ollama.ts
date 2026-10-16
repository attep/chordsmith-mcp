import type { Tool } from "./mcp";
import { resolve, variants, type JsonSchema } from "./schema";

export interface OllamaToolCall {
  function: { name: string; arguments: Record<string, unknown> | string };
}

export interface OllamaMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  tool_calls?: OllamaToolCall[];
  tool_name?: string;
}

export interface OllamaTool {
  type: "function";
  function: { name: string; description: string; parameters: JsonSchema };
}

const MAX_DEPTH = 6;
const KEEP = ["description", "enum", "minimum", "maximum", "format"] as const;

/**
 * Inline $refs and flatten anyOf/oneOf so small models (and Ollama's schema struct, which only
 * knows type/items/properties/required/enum/description) see a plain schema.
 */
export function simplifySchema(schema: JsonSchema, root: JsonSchema, depth = 0): JsonSchema {
  const resolved = resolve(schema, root);
  const out: JsonSchema = {};
  for (const key of KEEP) if (resolved[key] !== undefined) (out as Record<string, unknown>)[key] = resolved[key];
  if (resolved.default !== undefined && resolved.default !== null) {
    const hint = `Default: ${JSON.stringify(resolved.default)}.`;
    out.description = out.description ? `${out.description} ${hint}` : hint;
  }
  const { options } = variants(resolved, root);
  if (options.length > 1) {
    const enums = options.flatMap((option) => option.enum ?? (option.const !== undefined ? [option.const] : []));
    if (enums.length === options.length) {
      out.type = typeof enums[0] === "number" ? "number" : "string";
      out.enum = enums;
      return out;
    }
    const types = [...new Set(options.map((option) => simplifySchema(option, root, depth + 1).type as string))].filter(
      Boolean,
    );
    // Prefer the richest alternative's structure (e.g. ChordEvent over a bare chord string).
    const rich = options.find((option) => option.properties || option.items);
    if (rich) Object.assign(out, { ...simplifySchema(rich, root, depth + 1), description: out.description });
    out.type = types.length === 1 ? types[0] : types;
    return out;
  }
  const single = options[0] ?? resolved;
  if (single.const !== undefined) out.enum = [single.const];
  const type = Array.isArray(single.type) ? single.type.filter((t) => t !== "null")[0] : single.type;
  if (type) out.type = type;
  if (single.enum && !out.enum) out.enum = single.enum;
  if (depth >= MAX_DEPTH) return { type: out.type ?? "object", description: out.description };
  if (single.properties) {
    out.type = "object";
    out.properties = Object.fromEntries(
      Object.entries(single.properties).map(([key, child]) => [key, simplifySchema(child, root, depth + 1)]),
    );
    if (single.required?.length) out.required = single.required;
  }
  if (single.items) out.items = simplifySchema(single.items, root, depth + 1);
  return out;
}

export function toOllamaTool(tool: Tool): OllamaTool {
  const root = tool.inputSchema as JsonSchema;
  const parameters = simplifySchema(root, root);
  return {
    type: "function",
    function: {
      name: tool.name,
      description: tool.description ?? tool.title ?? tool.name,
      parameters: { type: "object", properties: parameters.properties ?? {}, required: parameters.required ?? [] },
    },
  };
}

/** Tool results can carry megabytes of base64; keep the model's context small. */
export function compactForModel(value: unknown, limit = 12000): string {
  const text = JSON.stringify(value, (key, child) =>
    typeof child === "string" && (key === "data_base64" || child.length > 2000)
      ? `<${child.length} characters omitted>`
      : child,
  );
  return text.length > limit ? `${text.slice(0, limit)}... (truncated)` : text;
}

export function parseArguments(args: Record<string, unknown> | string): Record<string, unknown> {
  if (typeof args !== "string") return args ?? {};
  try {
    const parsed = JSON.parse(args);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

export async function listModels(baseUrl: string): Promise<string[]> {
  const response = await fetch(`${baseUrl.replace(/\/+$/, "")}/api/tags`);
  if (!response.ok) throw new Error(`Ollama /api/tags: HTTP ${response.status}`);
  const data = (await response.json()) as { models?: { name: string }[] };
  return (data.models ?? []).map((model) => model.name).sort();
}

export async function chat(
  baseUrl: string,
  model: string,
  messages: OllamaMessage[],
  tools: OllamaTool[],
  signal?: AbortSignal,
): Promise<OllamaMessage> {
  const response = await fetch(`${baseUrl.replace(/\/+$/, "")}/api/chat`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model, messages, tools: tools.length ? tools : undefined, stream: false }),
    signal,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Ollama /api/chat: HTTP ${response.status} ${text.slice(0, 300)}`);
  }
  const data = (await response.json()) as { message: OllamaMessage };
  return data.message;
}
