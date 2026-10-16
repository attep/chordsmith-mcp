import { useCallback, useEffect, useState } from "react";
import { useApp } from "../lib/context";
import { resultData, resultText } from "../lib/mcp";

interface FileEntry {
  filename: string;
  kind: string;
  size_bytes: number;
  empty: boolean;
  modified: string;
}

interface Link {
  url: string;
  signed: boolean;
  expires_at?: number;
}

function base64Blob(data: string, mime: string): string {
  const bytes = Uint8Array.from(atob(data), (char) => char.charCodeAt(0));
  return URL.createObjectURL(new Blob([bytes], { type: mime }));
}

const formatSize = (bytes: number) =>
  bytes > 1 << 20 ? `${(bytes / (1 << 20)).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;

export function FilesView() {
  const { callTool, connectionState } = useApp();
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [outputDir, setOutputDir] = useState("");
  const [kind, setKind] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [links, setLinks] = useState<Record<string, Link>>({});
  const [linkErrors, setLinkErrors] = useState<Record<string, string>>({});

  const refresh = useCallback(async () => {
    const args: Record<string, unknown> = { limit: 500 };
    if (kind) args.kind = kind;
    if (name) args.name_contains = name;
    const result = await callTool("list_generated_files", args);
    if (result.isError) {
      setError(resultText(result));
      return;
    }
    const data = resultData(result) as { files: FileEntry[]; total: number; output_dir: string };
    setFiles(data.files);
    setTotal(data.total);
    setOutputDir(data.output_dir);
    setError(null);
  }, [callTool, kind, name]);

  useEffect(() => {
    if (connectionState === "connected") void refresh();
  }, [connectionState, refresh]);

  /** Prefer a signed /files/{filename} URL; without CHORDSMITH_PUBLIC_URL fall back to base64. */
  const fetchLink = async (file: FileEntry) => {
    const signed = await callTool("get_file", { filename: file.filename, return_as: "url", expires_in: 3600 });
    const data = resultData(signed) as { download_url?: string; expires_at?: number };
    if (!signed.isError && data.download_url) {
      setLinks((old) => ({ ...old, [file.filename]: { url: data.download_url!, signed: true, expires_at: data.expires_at } }));
      return;
    }
    const inline = await callTool("get_file", { filename: file.filename, return_as: "base64" });
    const payload = resultData(inline) as { data_base64?: string; mime_type?: string };
    if (inline.isError || !payload.data_base64) {
      setLinkErrors((old) => ({ ...old, [file.filename]: resultText(inline) || resultText(signed) }));
      return;
    }
    setLinks((old) => ({
      ...old,
      [file.filename]: { url: base64Blob(payload.data_base64!, payload.mime_type ?? "application/octet-stream"), signed: false },
    }));
  };

  return (
    <div className="files">
      <div className="row toolbar">
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">all kinds</option>
          <option value="midi">midi</option>
          <option value="wav">wav</option>
          <option value="mp3">mp3</option>
        </select>
        <input placeholder="name contains…" value={name} onChange={(e) => setName(e.target.value)} />
        <button onClick={() => void refresh()}>Refresh</button>
        <span className="muted">
          {files.length} of {total} in <code>{outputDir}</code>
        </span>
      </div>
      {error && <div className="banner error">{error}</div>}
      <table className="table">
        <thead>
          <tr>
            <th>File</th>
            <th>Kind</th>
            <th>Size</th>
            <th>Modified</th>
            <th>Download / play</th>
          </tr>
        </thead>
        <tbody>
          {files.map((file) => {
            const link = links[file.filename];
            const audio = file.kind === "wav" || file.kind === "mp3";
            return (
              <tr key={file.filename} className={file.empty ? "empty-file" : ""}>
                <td>
                  <code>{file.filename}</code>
                  {file.empty && <span className="badge danger">empty</span>}
                </td>
                <td>{file.kind}</td>
                <td>{formatSize(file.size_bytes)}</td>
                <td className="muted">{new Date(file.modified).toLocaleString()}</td>
                <td>
                  {!link && (
                    <button className="small" disabled={file.empty} onClick={() => void fetchLink(file)}>
                      {audio ? "Load player" : "Get link"}
                    </button>
                  )}
                  {link && (
                    <div className="file-actions">
                      {audio && <audio controls preload="none" src={link.url} />}
                      <a href={link.url} download={file.filename} target="_blank" rel="noreferrer">
                        download
                      </a>
                      <span className="muted small-text">{link.signed ? "signed URL" : "inline (no CHORDSMITH_PUBLIC_URL)"}</span>
                    </div>
                  )}
                  {linkErrors[file.filename] && <div className="error-text small-text">{linkErrors[file.filename]}</div>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
