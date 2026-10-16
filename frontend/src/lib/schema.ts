/** Small JSON Schema helpers for the form generator and the Ollama tool converter. */

export type JsonSchema = {
  type?: string | string[];
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  const?: unknown;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  anyOf?: JsonSchema[];
  oneOf?: JsonSchema[];
  allOf?: JsonSchema[];
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  definitions?: Record<string, JsonSchema>;
  additionalProperties?: boolean | JsonSchema;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  minItems?: number;
  maxItems?: number;
  format?: string;
  [key: string]: unknown;
};

/** Follow a local "#/$defs/X" reference (and allOf with one entry); sibling keywords win. */
export function resolve(schema: JsonSchema | undefined, root: JsonSchema): JsonSchema {
  let current: JsonSchema = schema ?? {};
  for (let depth = 0; depth < 16; depth++) {
    if (current.$ref) {
      const { $ref, ...rest } = current;
      current = { ...lookup($ref, root), ...rest };
    } else if (current.allOf && current.allOf.length === 1 && !current.properties) {
      const { allOf, ...rest } = current;
      current = { ...allOf[0], ...rest };
    } else {
      break;
    }
  }
  return current;
}

function lookup(ref: string, root: JsonSchema): JsonSchema {
  if (!ref.startsWith("#/")) return {};
  let node: unknown = root;
  for (const part of ref.slice(2).split("/")) {
    node = (node as Record<string, unknown> | undefined)?.[part.replace(/~1/g, "/").replace(/~0/g, "~")];
  }
  return (node as JsonSchema) ?? {};
}

export const isNullSchema = (schema: JsonSchema) =>
  schema.type === "null" || (schema.const === null && schema.type === undefined);

/** The non-null alternatives of an anyOf/oneOf/type-list, and whether null was allowed. */
export function variants(schema: JsonSchema, root: JsonSchema): { options: JsonSchema[]; nullable: boolean } {
  const list = schema.anyOf ?? schema.oneOf;
  if (list) {
    const resolved = list.map((entry) => resolve(entry, root));
    const options = resolved.filter((entry) => !isNullSchema(entry));
    return { options, nullable: options.length !== resolved.length };
  }
  if (Array.isArray(schema.type)) {
    const types = schema.type.filter((t) => t !== "null");
    return { options: types.map((type) => ({ ...schema, type })), nullable: types.length !== schema.type.length };
  }
  return { options: [schema], nullable: false };
}

export function primaryType(schema: JsonSchema): string | undefined {
  if (Array.isArray(schema.type)) return schema.type.find((t) => t !== "null");
  if (schema.type) return schema.type;
  if (schema.properties) return "object";
  if (schema.items) return "array";
  if (schema.enum) return typeof schema.enum[0] === "number" ? "number" : "string";
  return undefined;
}

export function variantLabel(schema: JsonSchema, index: number): string {
  if (schema.title) return schema.title;
  if (schema.const !== undefined) return String(schema.const);
  const type = primaryType(schema);
  if (type === "array" && schema.items) return `list`;
  return type ?? `option ${index + 1}`;
}

/** Which variant a value fits best (used when an anyOf field already holds a value). */
export function matchVariant(value: unknown, options: JsonSchema[]): number {
  const kind = Array.isArray(value) ? "array" : value === null ? "null" : typeof value;
  const index = options.findIndex((option) => {
    if (option.enum) return option.enum.includes(value);
    if (option.const !== undefined) return option.const === value;
    const type = primaryType(option);
    if (type === "integer" || type === "number") return kind === "number";
    if (type === "object") return kind === "object";
    return type === kind;
  });
  return Math.max(0, index);
}

/** A starting value for a schema: its default, else an empty value of its type. */
export function initialValue(schema: JsonSchema, root: JsonSchema): unknown {
  const resolved = resolve(schema, root);
  if (resolved.default !== undefined) return structuredClone(resolved.default);
  if (resolved.const !== undefined) return resolved.const;
  const { options, nullable } = variants(resolved, root);
  if (nullable) return undefined;
  const target = options[0] ?? resolved;
  if (target.enum) return target.enum[0];
  switch (primaryType(target)) {
    case "string":
      return "";
    case "boolean":
      return false;
    case "array":
      return [];
    case "object": {
      const value: Record<string, unknown> = {};
      for (const key of target.required ?? []) {
        value[key] = initialValue(target.properties?.[key] ?? {}, root);
      }
      return value;
    }
    default:
      return undefined;
  }
}

/** Drop undefined entries (omitted optional fields) before sending arguments. */
export function prune(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(prune);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .filter(([, child]) => child !== undefined)
        .map(([key, child]) => [key, prune(child)]),
    );
  }
  return value;
}
