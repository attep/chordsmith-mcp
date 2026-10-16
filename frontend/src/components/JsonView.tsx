import type { CallToolResult } from "../lib/mcp";
import { resultData } from "../lib/mcp";

export function JsonView({ value, maxHeight }: { value: unknown; maxHeight?: number }) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return (
    <pre className="json" style={maxHeight ? { maxHeight } : undefined}>
      {text}
    </pre>
  );
}

export function ToolResultView({ result, maxHeight }: { result: CallToolResult; maxHeight?: number }) {
  return (
    <div className={result.isError ? "result is-error" : "result"}>
      <div className="result-head">{result.isError ? "isError: true" : "OK"}</div>
      <JsonView value={resultData(result)} maxHeight={maxHeight} />
    </div>
  );
}
