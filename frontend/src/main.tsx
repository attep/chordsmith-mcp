import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { AppProvider } from "./lib/context";
import { applyStartupLink } from "./lib/startup";
import "./styles.css";

// A start-stack link carries the tunnel URL and a fresh token; adopt them before mounting.
applyStartupLink();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppProvider>
      <App />
    </AppProvider>
  </StrictMode>,
);
