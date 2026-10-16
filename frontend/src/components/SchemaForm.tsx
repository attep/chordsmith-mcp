import { useEffect, useState } from "react";
import {
  initialValue,
  matchVariant,
  primaryType,
  resolve,
  variantLabel,
  variants,
  type JsonSchema,
} from "../lib/schema";

interface FieldProps {
  name: string;
  schema: JsonSchema;
  root: JsonSchema;
  value: unknown;
  onChange: (value: unknown) => void;
  required: boolean;
  depth: number;
}

/** A form for a tool's JSON inputSchema; `value` is the arguments object. */
export function SchemaForm({
  schema,
  value,
  onChange,
}: {
  schema: JsonSchema;
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
}) {
  const root = resolve(schema, schema);
  const properties = root.properties ?? {};
  const required = new Set(root.required ?? []);
  if (!Object.keys(properties).length) return <p className="muted">This tool takes no arguments.</p>;
  return (
    <div className="schema-form">
      {Object.entries(properties).map(([key, child]) => (
        <Field
          key={key}
          name={key}
          schema={child}
          root={schema}
          value={value[key]}
          required={required.has(key)}
          depth={1}
          onChange={(next) => onChange({ ...value, [key]: next })}
        />
      ))}
    </div>
  );
}

function Label({ name, schema, required }: { name: string; schema: JsonSchema; required: boolean }) {
  return (
    <div className="field-label">
      <span className="field-name">
        {name}
        {required && <span className="req">*</span>}
      </span>
      {schema.description && <span className="field-desc">{schema.description}</span>}
    </div>
  );
}

function Field(props: FieldProps) {
  const { name, root, value, onChange, required, depth } = props;
  const schema = resolve(props.schema, root);
  const { options, nullable } = variants(schema, root);
  const optional = !required || nullable;

  if (options.length > 1) {
    const allConst = options.every((option) => option.const !== undefined || option.enum);
    if (allConst) {
      const merged = { ...schema, enum: options.flatMap((option) => option.enum ?? [option.const]) };
      return <EnumField {...props} schema={merged} optional={optional} />;
    }
    const index = value === undefined ? -1 : matchVariant(value, options);
    return (
      <div className="field">
        <Label name={name} schema={schema} required={required} />
        <select
          className="variant"
          value={index}
          onChange={(event) => {
            const next = Number(event.target.value);
            onChange(next < 0 ? undefined : (initialValue(options[next], root) ?? emptyOf(options[next])));
          }}
        >
          {optional && <option value={-1}>(omit{defaultHint(schema)})</option>}
          {options.map((option, i) => (
            <option key={i} value={i}>
              {variantLabel(option, i)}
            </option>
          ))}
        </select>
        {index >= 0 && (
          <div className="variant-body">
            <Field {...props} name={variantLabel(options[index], index)} schema={options[index]} required depth={depth + 1} />
          </div>
        )}
      </div>
    );
  }

  const target: JsonSchema = {
    ...options[0],
    description: schema.description ?? options[0]?.description,
    default: schema.default ?? options[0]?.default,
  };
  const type = primaryType(target);
  if (target.enum) return <EnumField {...props} schema={target} optional={optional} />;
  if (type === "object" && target.properties) return <ObjectField {...props} schema={target} optional={optional} />;
  if (type === "array") return <ArrayField {...props} schema={target} optional={optional} />;
  if (type === "boolean") return <BooleanField {...props} schema={target} optional={optional} />;
  if (type === "integer" || type === "number") return <NumberField {...props} schema={target} integer={type === "integer"} />;
  if (type === "string") return <StringField {...props} schema={target} optional={optional} />;
  return <JsonField {...props} schema={target} />;
}

const emptyOf = (schema: JsonSchema): unknown =>
  ({ string: "", array: [], object: {}, boolean: false })[primaryType(schema) ?? ""] ?? undefined;

const defaultHint = (schema: JsonSchema) =>
  schema.default !== undefined && schema.default !== null ? `, default ${JSON.stringify(schema.default)}` : "";

function EnumField({ name, schema, value, onChange, required, optional }: FieldProps & { optional: boolean }) {
  const values = schema.enum ?? [];
  const index = values.findIndex((entry) => entry === value);
  return (
    <label className="field">
      <Label name={name} schema={schema} required={required} />
      <select value={index} onChange={(event) => onChange(Number(event.target.value) < 0 ? undefined : values[Number(event.target.value)])}>
        {(optional || index < 0) && <option value={-1}>(omit{defaultHint(schema)})</option>}
        {values.map((entry, i) => (
          <option key={i} value={i}>
            {String(entry)}
          </option>
        ))}
      </select>
    </label>
  );
}

function StringField({ name, schema, value, onChange, required, optional }: FieldProps & { optional: boolean }) {
  const multiline = /base64|lyrics|text|data/i.test(name);
  const common = {
    value: typeof value === "string" ? value : "",
    placeholder: schema.default !== undefined && schema.default !== null ? String(schema.default) : "",
    onChange: (event: { target: { value: string } }) =>
      onChange(event.target.value === "" && optional ? undefined : event.target.value),
  };
  return (
    <label className="field">
      <Label name={name} schema={schema} required={required} />
      {multiline ? <textarea rows={3} {...common} /> : <input type="text" {...common} />}
    </label>
  );
}

function NumberField({ name, schema, value, onChange, required, integer }: FieldProps & { integer: boolean }) {
  const min = schema.minimum ?? schema.exclusiveMinimum;
  const max = schema.maximum ?? schema.exclusiveMaximum;
  return (
    <label className="field">
      <Label name={name} schema={schema} required={required} />
      <input
        type="number"
        step={integer ? 1 : "any"}
        min={min}
        max={max}
        value={typeof value === "number" ? value : ""}
        placeholder={schema.default !== undefined && schema.default !== null ? String(schema.default) : ""}
        onChange={(event) => onChange(event.target.value === "" ? undefined : Number(event.target.value))}
      />
      {(min !== undefined || max !== undefined) && (
        <span className="hint">
          {min ?? "…"} – {max ?? "…"}
        </span>
      )}
    </label>
  );
}

function BooleanField({ name, schema, value, onChange, required, optional }: FieldProps & { optional: boolean }) {
  if (optional) {
    return (
      <label className="field">
        <Label name={name} schema={schema} required={required} />
        <select
          value={value === undefined ? "" : String(value)}
          onChange={(event) => onChange(event.target.value === "" ? undefined : event.target.value === "true")}
        >
          <option value="">(omit{defaultHint(schema)})</option>
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      </label>
    );
  }
  return (
    <label className="field checkbox">
      <input type="checkbox" checked={value === true} onChange={(event) => onChange(event.target.checked)} />
      <Label name={name} schema={schema} required={required} />
    </label>
  );
}

function ObjectField({ name, schema, root, value, onChange, required, depth, optional }: FieldProps & { optional: boolean }) {
  const included = value !== undefined && value !== null;
  const record = (included ? value : {}) as Record<string, unknown>;
  const requiredKeys = new Set(schema.required ?? []);
  return (
    <details className="object-field" open={depth <= 1 && included}>
      <summary>
        <span className="field-name">
          {name}
          {required && !optional && <span className="req">*</span>}
        </span>
        {optional && (
          <label className="include" onClick={(event) => event.stopPropagation()}>
            <input
              type="checkbox"
              checked={included}
              onChange={(event) => onChange(event.target.checked ? (initialValue({ ...schema, default: undefined }, root) ?? {}) : undefined)}
            />
            include
          </label>
        )}
        {schema.description && <span className="field-desc">{schema.description}</span>}
      </summary>
      <fieldset disabled={!included}>
        {Object.entries(schema.properties ?? {}).map(([key, child]) => (
          <Field
            key={key}
            name={key}
            schema={child}
            root={root}
            value={record[key]}
            required={requiredKeys.has(key)}
            depth={depth + 1}
            onChange={(next) => onChange({ ...record, [key]: next })}
          />
        ))}
      </fieldset>
    </details>
  );
}

function ArrayField({ name, schema, root, value, onChange, required, depth }: FieldProps & { optional: boolean }) {
  const items = Array.isArray(value) ? value : [];
  const itemSchema = schema.items ?? {};
  const add = () => onChange([...items, initialValue(itemSchema, root) ?? emptyOf(resolve(itemSchema, root))]);
  return (
    <div className="field array-field">
      <Label name={name} schema={schema} required={required} />
      {items.map((item, index) => (
        <div className="array-item" key={index}>
          <Field
            name={`${name}[${index}]`}
            schema={itemSchema}
            root={root}
            value={item}
            required
            depth={depth + 1}
            onChange={(next) => onChange(items.map((old, i) => (i === index ? next : old)))}
          />
          <button type="button" className="small danger" onClick={() => onChange(items.filter((_, i) => i !== index))}>
            ✕
          </button>
        </div>
      ))}
      <div className="row">
        <button type="button" className="small" onClick={add}>
          + add item
        </button>
        {value !== undefined && !required && (
          <button type="button" className="small" onClick={() => onChange(undefined)}>
            omit
          </button>
        )}
      </div>
    </div>
  );
}

function JsonField({ name, schema, value, onChange, required }: FieldProps) {
  const [text, setText] = useState(value === undefined ? "" : JSON.stringify(value, null, 2));
  const [invalid, setInvalid] = useState(false);
  useEffect(() => {
    if (value === undefined) setText("");
  }, [value]);
  return (
    <label className="field">
      <Label name={name} schema={schema} required={required} />
      <textarea
        rows={3}
        className={invalid ? "invalid" : ""}
        value={text}
        placeholder="JSON"
        onChange={(event) => {
          setText(event.target.value);
          if (event.target.value.trim() === "") {
            setInvalid(false);
            onChange(undefined);
            return;
          }
          try {
            onChange(JSON.parse(event.target.value));
            setInvalid(false);
          } catch {
            setInvalid(true);
          }
        }}
      />
    </label>
  );
}
