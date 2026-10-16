import { useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../lib/context";
import { prepareToolArguments } from "../lib/arguments";
import { resultData, type CallToolResult } from "../lib/mcp";
import {
  chat,
  compactForModel,
  listModels,
  parseArguments,
  toOllamaTool,
  type OllamaMessage,
} from "../lib/ollama";
import { JsonView, ToolResultView } from "./JsonView";

type ChatItem =
  | { kind: "message"; message: OllamaMessage }
  | {
      kind: "tool";
      name: string;
      args: Record<string, unknown>;
      result?: CallToolResult;
      denied?: boolean;
      repaired?: string[];
      badArguments?: string;
    }
  | { kind: "error"; text: string };

const MAX_ROUNDS = 8;
const TOOLS_KEY = "chordsmith-manager.allowedTools";

const SYSTEM_PROMPT =
  "You are a music-production assistant connected to the ChordSmith MCP server. Use the tools to " +
  "create and inspect MIDI and audio files; you choose the music, the tools write the files. Call " +
  "tools with valid JSON arguments and summarise the results for the user.";

function loadAllowed(): { restrict: boolean; names: string[] } {
  try {
    return JSON.parse(localStorage.getItem(TOOLS_KEY) ?? "") as { restrict: boolean; names: string[] };
  } catch {
    return { restrict: false, names: [] };
  }
}

export function Assistant() {
  const { settings, updateSettings, tools, callTool, connection } = useApp();
  const [models, setModels] = useState<string[]>([]);
  const [modelError, setModelError] = useState<string | null>(null);
  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [allowed, setAllowed] = useState(loadAllowed);
  const [showTools, setShowTools] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const refreshModels = async () => {
    try {
      const names = await listModels(settings.ollamaUrl);
      setModels(names);
      setModelError(null);
      if (!settings.ollamaModel && names.length) updateSettings({ ...settings, ollamaModel: names[0] });
    } catch (error) {
      setModelError(String(error instanceof Error ? error.message : error));
    }
  };

  useEffect(() => {
    void refreshModels();
  }, [settings.ollamaUrl]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    localStorage.setItem(TOOLS_KEY, JSON.stringify(allowed));
  }, [allowed]);

  useEffect(() => {
    // scrollIntoView's value must not be returned: React treats any return as a cleanup
    // function and crashes with "destroy is not a function" (React 19 dev double-invoke).
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [items]);

  const enabledTools = useMemo(
    () => (allowed.restrict ? tools.filter((tool) => allowed.names.includes(tool.name)) : tools),
    [tools, allowed],
  );

  const toggleTool = (name: string) =>
    setAllowed((old) => ({
      ...old,
      names: old.names.includes(name) ? old.names.filter((n) => n !== name) : [...old.names, name],
    }));

  const toMessages = (history: ChatItem[]): OllamaMessage[] => {
    const system = [SYSTEM_PROMPT, connection?.instructions].filter(Boolean).join("\n\n");
    const messages: OllamaMessage[] = [{ role: "system", content: system }];
    for (const item of history) {
      if (item.kind === "message") messages.push(item.message);
      if (item.kind === "tool") {
        const content = item.denied
          ? `Tool '${item.name}' is not enabled for you in this chat; do not call it again.`
          : item.badArguments
            ? item.badArguments
            : compactForModel({ isError: item.result?.isError ?? false, result: item.result ? resultData(item.result) : null });
        messages.push({ role: "tool", content, tool_name: item.name });
      }
    }
    return messages;
  };

  const send = async () => {
    const text = input.trim();
    if (!text || busy || !settings.ollamaModel) return;
    setInput("");
    setBusy(true);
    const controller = new AbortController();
    abortRef.current = controller;
    let history: ChatItem[] = [...items, { kind: "message", message: { role: "user", content: text } }];
    setItems(history);
    const ollamaTools = enabledTools.map(toOllamaTool);
    const allowedNames = new Set(enabledTools.map((tool) => tool.name));
    const toolByName = new Map(tools.map((tool) => [tool.name, tool]));
    try {
      for (let round = 0; round < MAX_ROUNDS; round++) {
        const reply = await chat(settings.ollamaUrl, settings.ollamaModel, toMessages(history), ollamaTools, controller.signal);
        history = [...history, { kind: "message", message: reply }];
        setItems(history);
        const calls = reply.tool_calls ?? [];
        if (!calls.length) break;
        for (const call of calls) {
          const name = call.function.name;
          if (!allowedNames.has(name)) {
            history = [...history, { kind: "tool", name, args: parseArguments(call.function.arguments), denied: true }];
            setItems(history);
            continue;
          }
          const tool = toolByName.get(name);
          const prepared = tool
            ? prepareToolArguments(tool, call.function.arguments)
            : { args: parseArguments(call.function.arguments), repaired: [] as string[], error: undefined };
          if (prepared.error) {
            // don't send a broken call: the message goes back to the model so it can retry
            history = [...history, { kind: "tool", name, args: prepared.args, badArguments: prepared.error }];
            setItems(history);
            continue;
          }
          const pending: ChatItem = { kind: "tool", name, args: prepared.args, repaired: prepared.repaired };
          setItems([...history, pending]);
          const result = await callTool(name, prepared.args);
          history = [...history, { ...pending, result }];
          setItems(history);
        }
        if (round === MAX_ROUNDS - 1) {
          history = [...history, { kind: "error", text: `Stopped after ${MAX_ROUNDS} tool rounds.` }];
          setItems(history);
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        history = [...history, { kind: "error", text: String(error instanceof Error ? error.message : error) }];
        setItems(history);
      }
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  };

  return (
    <div className="assistant">
      <div className="row toolbar">
        <label>
          Model{" "}
          <select
            value={settings.ollamaModel}
            onChange={(e) => updateSettings({ ...settings, ollamaModel: e.target.value })}
          >
            {!models.includes(settings.ollamaModel) && <option value={settings.ollamaModel}>{settings.ollamaModel || "(none)"}</option>}
            {models.map((model) => (
              <option key={model} value={model}>
                {model}
              </option>
            ))}
          </select>
        </label>
        <button className="small" onClick={() => void refreshModels()}>
          ↻ models
        </button>
        <label className="inline">
          <input
            type="checkbox"
            checked={allowed.restrict}
            onChange={(e) => setAllowed((old) => ({ ...old, restrict: e.target.checked }))}
          />{" "}
          restrict tools
        </label>
        <button className="small" onClick={() => setShowTools((v) => !v)}>
          {enabledTools.length}/{tools.length} tools enabled {showTools ? "▲" : "▼"}
        </button>
        <button className="small" onClick={() => setItems([])} disabled={busy}>
          clear chat
        </button>
        <span className="muted small-text">{settings.ollamaUrl}</span>
      </div>
      {modelError && (
        <div className="banner error">
          {modelError}. Is Ollama running at {settings.ollamaUrl}? Browsers need the page origin in{" "}
          <code>OLLAMA_ORIGINS</code> (localhost origins are allowed by default).
        </div>
      )}
      {showTools && (
        <div className="tool-picker">
          <div className="row">
            <button className="small" onClick={() => setAllowed({ restrict: true, names: tools.map((t) => t.name) })}>
              all
            </button>
            <button
              className="small"
              onClick={() =>
                setAllowed({ restrict: true, names: tools.filter((t) => t.annotations?.readOnlyHint).map((t) => t.name) })
              }
            >
              read-only only
            </button>
            <button className="small" onClick={() => setAllowed({ restrict: true, names: [] })}>
              none
            </button>
          </div>
          <div className="tool-checks">
            {tools.map((tool) => (
              <label key={tool.name} className={allowed.restrict ? "" : "muted"}>
                <input
                  type="checkbox"
                  disabled={!allowed.restrict}
                  checked={!allowed.restrict || allowed.names.includes(tool.name)}
                  onChange={() => toggleTool(tool.name)}
                />
                {tool.name}
              </label>
            ))}
          </div>
        </div>
      )}
      <div className="chat">
        {items.length === 0 && (
          <p className="muted">
            Ask for something like “write a melancholic 8-bar progression in A minor with an arpeggio rhythm”. The model
            sees the enabled MCP tools; its tool calls run through this page's MCP session.
          </p>
        )}
        {items.map((item, index) => {
          if (item.kind === "error") return <div key={index} className="bubble error">{item.text}</div>;
          if (item.kind === "tool") {
            return (
              <div key={index} className={`tool-call ${item.denied || item.badArguments ? "denied" : ""}`}>
                <div className="tool-call-head">
                  🔧 <code>{item.name}</code>
                  {item.denied && <span className="badge danger">blocked (not enabled)</span>}
                  {item.badArguments && <span className="badge danger">bad arguments (asked the model to retry)</span>}
                  {item.repaired && item.repaired.length > 0 && (
                    <span className="badge" title="fixed against the tool's schema before the call">
                      repaired: {item.repaired.join(", ")}
                    </span>
                  )}
                  {!item.denied && !item.badArguments && !item.result && <span className="muted"> running…</span>}
                </div>
                <details>
                  <summary>arguments</summary>
                  <JsonView value={item.args} />
                </details>
                {item.badArguments && <div className="error-text">{item.badArguments}</div>}
                {item.result && (
                  <details open={item.result.isError}>
                    <summary className={item.result.isError ? "error-text" : ""}>
                      result{item.result.isError ? " (isError)" : ""}
                    </summary>
                    <ToolResultView result={item.result} maxHeight={300} />
                  </details>
                )}
              </div>
            );
          }
          const { message } = item;
          if (!message.content && message.tool_calls?.length) return null;
          return (
            <div key={index} className={`bubble ${message.role}`}>
              <div className="pre-wrap">{message.content}</div>
            </div>
          );
        })}
        {busy && <div className="muted">thinking…</div>}
        <div ref={bottomRef} />
      </div>
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <textarea
          rows={2}
          value={input}
          placeholder={settings.ollamaModel ? `Message ${settings.ollamaModel}…` : "Pick a model first"}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
        />
        {busy ? (
          <button type="button" className="danger" onClick={() => abortRef.current?.abort()}>
            Stop
          </button>
        ) : (
          <button type="submit" className="primary" disabled={!settings.ollamaModel}>
            Send
          </button>
        )}
      </form>
    </div>
  );
}
