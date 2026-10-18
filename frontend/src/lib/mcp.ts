import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { CallToolResult, Tool } from "@modelcontextprotocol/sdk/types.js";
import { authHeaders, serverEndpoint, type Settings } from "./settings";
import { withAuthRetry } from "./token";

export type { CallToolResult, Tool };

export const CLIENT_INFO = { name: "chordsmith-manager", version: "0.1.0" };

/** Resolves the bearer token for a request; `force` skips the cache and renews immediately. */
export type TokenSource = (force?: boolean) => Promise<string | undefined>;

export type FetchLike = (url: string | URL, init?: RequestInit) => Promise<Response>;

/**
 * A fetch that adds the current bearer token to every request. The MCP session outlives the
 * short-lived access tokens, so the header is resolved per request instead of being frozen at
 * connect time (a cached token is free; a nearly expired one is renewed transparently).
 */
export function bearerFetch(
  baseHeaders: Record<string, string>,
  tokenSource?: TokenSource,
  seen?: { token?: string },
): FetchLike {
  return async (url, init) => {
    const headers = new Headers(init?.headers);
    for (const [name, value] of Object.entries(baseHeaders)) headers.set(name, value);
    if (tokenSource) {
      const token = await tokenSource();
      if (token) {
        headers.set("Authorization", `Bearer ${token}`);
        if (seen) seen.token = token;
      } else {
        headers.delete("Authorization");
      }
    }
    return fetch(url, { ...init, headers });
  };
}

/** One streamable-HTTP MCP session to the ChordSmith server. */
export class McpConnection {
  private constructor(
    private readonly client: Client,
    private readonly transport: StreamableHTTPClientTransport,
    private readonly endpoint: string,
    private readonly headers: Record<string, string>,
    private readonly tokenSource: TokenSource | undefined,
    private readonly lastToken: { token?: string },
  ) {}

  static async open(settings: Settings, tokenSource?: TokenSource): Promise<McpConnection> {
    const endpoint = serverEndpoint(settings, settings.mcpPath);
    const headers = authHeaders(settings);
    const lastToken: { token?: string } = {};
    const transport = new StreamableHTTPClientTransport(new URL(endpoint), {
      fetch: bearerFetch(headers, tokenSource, lastToken),
    });
    const client = new Client(CLIENT_INFO);
    await client.connect(transport);
    return new McpConnection(client, transport, endpoint, headers, tokenSource, lastToken);
  }

  /** Runs a request with the current token; a rejected token is renewed once and retried. */
  private runWithToken<T>(run: () => Promise<T>): Promise<T> {
    if (!this.tokenSource) return run();
    return withAuthRetry(this.tokenSource, run);
  }

  /** Keeps the session marked active on the server (its event stream alone can drop). */
  async ping(): Promise<void> {
    await this.runWithToken(() => this.client.ping());
  }

  /** Best-effort session DELETE while the page unloads; a keepalive fetch outlives the page. */
  abandon(): void {
    const sessionId = this.transport.sessionId;
    if (!sessionId) return;
    const headers: Record<string, string> = { ...this.headers, "mcp-session-id": sessionId };
    if (this.lastToken.token) headers.Authorization = `Bearer ${this.lastToken.token}`;
    if (this.transport.protocolVersion) headers["mcp-protocol-version"] = this.transport.protocolVersion;
    fetch(this.endpoint, { method: "DELETE", headers, keepalive: true }).catch(() => undefined);
  }

  get sessionId(): string | undefined {
    return this.transport.sessionId;
  }

  get serverVersion() {
    return this.client.getServerVersion();
  }

  get instructions(): string | undefined {
    return this.client.getInstructions();
  }

  async listTools(): Promise<Tool[]> {
    const tools: Tool[] = [];
    let cursor: string | undefined;
    do {
      const page = await this.client.listTools(cursor ? { cursor } : undefined);
      tools.push(...page.tools);
      cursor = page.nextCursor;
    } while (cursor);
    return tools;
  }

  /** Call a tool; protocol/transport failures become an isError result so the UI shows them. */
  async callTool(name: string, args: Record<string, unknown>): Promise<CallToolResult> {
    try {
      return (await this.runWithToken(() => this.client.callTool({ name, arguments: args }))) as CallToolResult;
    } catch (error) {
      return { isError: true, content: [{ type: "text", text: String(error instanceof Error ? error.message : error) }] };
    }
  }

  async close(): Promise<void> {
    try {
      await this.transport.terminateSession();
    } catch {
      // the server may already be gone
    }
    await this.client.close();
  }
}

/** The useful payload of a tool result: structuredContent, else JSON-parsed text, else the text. */
export function resultData(result: CallToolResult): unknown {
  if (result.structuredContent !== undefined && result.structuredContent !== null) return result.structuredContent;
  const texts = result.content.filter((part) => part.type === "text").map((part) => part.text);
  if (texts.length === 1) {
    try {
      return JSON.parse(texts[0]);
    } catch {
      return texts[0];
    }
  }
  return texts.join("\n");
}

export function resultText(result: CallToolResult): string {
  return result.content
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("\n");
}
