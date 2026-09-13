import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, File, Folder, FolderOpen, RefreshCw } from "lucide-react";
import { get, type Directory } from "./api";

function DirectoryTree({ path, version, sessionId }: { path: string; version: number; sessionId?: string }) {
  const [directory, setDirectory] = useState<Directory>();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    const query = `path=${encodeURIComponent(path)}${sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : ""}`;
    get<Directory>(`/api/workspace?${query}`).then((data) => {
      if (active) { setDirectory(data); setError(""); }
    }).catch((error) => { if (active) setError(String(error)); });
    return () => { active = false; };
  }, [path, version]);

  if (error) return <p className="tree-error" role="alert">{error}</p>;
  if (!directory) return <p className="tree-loading">加载中…</p>;

  const entries = [...directory.entries].sort((a, b) =>
    Number(b.type === "directory") - Number(a.type === "directory") || a.name.localeCompare(b.name),
  );

  return (
    <>
      {path === "." && <div className="workspace-root" title={directory.root}><FolderOpen size={16} /><span>{directory.root.split("/").pop() || directory.root}</span></div>}
      <ul className="file-tree">
        {entries.map((entry) => {
          const childPath = path === "." ? entry.name : `${path}/${entry.name}`;
          const isOpen = expanded.has(entry.name);
          return <li key={entry.name}>
            {entry.type === "directory" ? <>
              <button className="tree-entry" aria-expanded={isOpen} onClick={() => setExpanded((previous) => {
                const next = new Set(previous);
                if (isOpen) next.delete(entry.name); else next.add(entry.name);
                return next;
              })}>
                {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                {isOpen ? <FolderOpen size={15} /> : <Folder size={15} />}<span>{entry.name}</span>
              </button>
              {isOpen && <DirectoryTree path={childPath} version={version} sessionId={sessionId} />}
            </> : <div className="tree-entry file-entry" title={childPath}><File size={15} /><span>{entry.name}</span>{entry.type === "symlink" && <span>↗</span>}</div>}
          </li>;
        })}
        {entries.length === 0 && <li className="empty-directory">空目录</li>}
      </ul>
    </>
  );
}

export function Workspace({ version, sessionId }: { version: number; sessionId?: string }) {
  const [refresh, setRefresh] = useState(0);
  return <aside className="workspace-panel" aria-label="Workspace">
    <header className="workspace-header"><span>Workspace</span><button className="icon-button" aria-label="刷新目录" onClick={() => setRefresh((value) => value + 1)}><RefreshCw size={14} /></button></header>
    <div className="workspace-files"><DirectoryTree path="." version={version + refresh} sessionId={sessionId} /></div>
    <div className="workspace-footer"><Folder size={13} /> 项目文件</div>
  </aside>;
}
