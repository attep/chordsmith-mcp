import { useState } from "react";
import { Assistant } from "./components/Assistant";
import { Dashboard } from "./components/Dashboard";
import { FilesView } from "./components/FilesView";
import { SettingsPanel } from "./components/SettingsPanel";
import { ToolRunner } from "./components/ToolRunner";
import { useApp } from "./lib/context";

const TABS = ["Dashboard", "Tools", "Files", "Assistant", "Settings"] as const;
type Tab = (typeof TABS)[number];

export function App() {
  const { connectionState, connectionError, settings, reconnect, tools } = useApp();
  const [tab, setTab] = useState<Tab>("Dashboard");
  const [toolRequest, setToolRequest] = useState<{ name: string; args: Record<string, unknown> } | null>(null);

  const openTool = (name: string, args: Record<string, unknown>) => {
    setToolRequest({ name, args });
    setTab("Tools");
  };

  return (
    <div className="app">
      <header className="topbar">
        <h1>ChordSmith Manager</h1>
        <nav>
          {TABS.map((name) => (
            <button key={name} className={name === tab ? "tab active" : "tab"} onClick={() => setTab(name)}>
              {name}
            </button>
          ))}
        </nav>
        <div className={`conn conn-${connectionState}`} title={connectionError ?? undefined}>
          <span className="dot" />
          {connectionState === "connected" ? `${tools.length} tools · ${settings.serverUrl}` : connectionState}
          {connectionState === "error" && (
            <button className="small" onClick={reconnect}>
              Retry
            </button>
          )}
        </div>
      </header>
      {connectionState === "error" && (
        <div className="banner error">
          Could not connect to {settings.serverUrl}
          {settings.mcpPath}: {connectionError}. Is <code>chordsmith-mcp --transport streamable-http</code> running
          (with <code>CHORDSMITH_AUTH=off</code> or a bearer token in Settings)?
        </div>
      )}
      <main>
        {tab === "Dashboard" && <Dashboard onOpenTool={openTool} />}
        {tab === "Tools" && <ToolRunner request={toolRequest} />}
        {tab === "Files" && <FilesView />}
        {tab === "Assistant" && <Assistant />}
        {tab === "Settings" && <SettingsPanel />}
      </main>
    </div>
  );
}
