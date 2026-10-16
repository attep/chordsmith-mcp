import { useState } from "react";
import { useApp } from "../lib/context";
import { DEFAULT_SETTINGS, type Settings } from "../lib/settings";

const FIELDS: { key: keyof Settings; label: string; hint: string; type?: string }[] = [
  { key: "serverUrl", label: "ChordSmith server URL", hint: "Where chordsmith-mcp --transport streamable-http listens." },
  { key: "mcpPath", label: "MCP endpoint path", hint: "Streamable-HTTP path (default /mcp)." },
  {
    key: "bearerToken",
    label: "Bearer token (optional)",
    hint: "Only needed when the server runs with OAuth on; locally, start it with CHORDSMITH_AUTH=off.",
    type: "password",
  },
  { key: "ollamaUrl", label: "Ollama base URL", hint: "Used by the Assistant tab (/api/tags, /api/chat)." },
  { key: "pollMs", label: "Dashboard poll interval (ms)", hint: "How often /api/status and the get_*_job tools are polled.", type: "number" },
];

export function SettingsPanel() {
  const { settings, updateSettings, reconnect } = useApp();
  const [draft, setDraft] = useState<Settings>(settings);

  const save = () => {
    updateSettings({ ...draft, pollMs: Number(draft.pollMs) || DEFAULT_SETTINGS.pollMs });
    reconnect();
  };

  return (
    <form
      className="settings"
      onSubmit={(event) => {
        event.preventDefault();
        save();
      }}
    >
      {FIELDS.map((field) => (
        <label key={field.key} className="field">
          <div className="field-label">
            <span className="field-name">{field.label}</span>
            <span className="field-desc">{field.hint}</span>
          </div>
          <input
            type={field.type ?? "text"}
            value={String(draft[field.key])}
            onChange={(e) => setDraft({ ...draft, [field.key]: field.type === "number" ? Number(e.target.value) : e.target.value })}
          />
        </label>
      ))}
      <div className="row">
        <button type="submit" className="primary">
          Save &amp; reconnect
        </button>
        <button type="button" onClick={() => setDraft({ ...DEFAULT_SETTINGS })}>
          Defaults
        </button>
      </div>
      <p className="muted">Settings, saved job ids and the assistant's tool choices are stored in this browser's localStorage.</p>
    </form>
  );
}
