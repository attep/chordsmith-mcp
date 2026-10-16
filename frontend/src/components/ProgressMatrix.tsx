import type { JobRow } from "./Dashboard";

export function ProgressMatrix({ rows, onOpen }: { rows: JobRow[]; onOpen: (row: JobRow) => void }) {
  const columns: string[] = [];
  for (const row of rows) for (const track of Object.keys(row.progress)) if (!columns.includes(track)) columns.push(track);
  if (!rows.length) return <p className="muted">No jobs.</p>;
  return (
    <div className="matrix-wrap">
      <table className="matrix">
        <thead>
          <tr>
            <th>Job</th>
            <th>Kind</th>
            <th>Status</th>
            <th>Created</th>
            {columns.map((track) => (
              <th key={track} className="track">
                {track}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.job_id}>
              <td>
                <button className="link" onClick={() => onOpen(row)}>
                  {row.job_id}
                </button>
                {row.detail && <div className="muted small-text">{row.detail}</div>}
                {row.error && <div className="error-text small-text">{row.error}</div>}
              </td>
              <td>{row.kind}</td>
              <td>
                <span className={`state state-${row.status}`}>{row.status}</span>
              </td>
              <td className="muted">{row.created_at ? new Date(row.created_at).toLocaleTimeString() : "–"}</td>
              {columns.map((track) => {
                const state = row.progress[track];
                return (
                  <td key={track} className={state ? `cell cell-${state}` : "cell"} title={row.errors[track] ?? state ?? ""}>
                    {state ?? ""}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
