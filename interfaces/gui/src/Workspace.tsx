import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, File, Folder, FolderOpen, PanelRightClose, RefreshCw } from "lucide-react";
import { get, type Directory } from "./api";
import { CopyTextButton } from "./CopyTextButton";

function DirectoryTree({ path, version, sessionId }: { path: string; version: number; sessionId?: string }) {
  const [directory, setDirectory] = useState<Directory>();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [loadingMore, setLoadingMore] = useState(false);

  useEffect(() => {
    let active = true;
    const query = `path=${encodeURIComponent(path)}${sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : ""}`;
    get<Directory>(`/api/workspace?${query}`).then((data) => {
      if (active) { setDirectory(data); setError(""); }
    }).catch((error) => { if (active) setError(String(error)); });
    return () => { active = false; };
  }, [path, version, sessionId]);

  if (error) return <p className="tree-error" role="alert">{error}</p>;
  if (!directory) return <p className="tree-loading">加载中…</p>;

  const entries = [...directory.entries].sort((a, b) =>
    Number(b.type === "directory") - Number(a.type === "directory") || a.name.localeCompare(b.name),
  );

  const loadMore = async () => {
    if (!directory.next_cursor || loadingMore) return;
    setLoadingMore(true);
    const query = `path=${encodeURIComponent(path)}&cursor=${encodeURIComponent(directory.next_cursor)}${sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : ""}`;
    try {
      const next = await get<Directory>(`/api/workspace?${query}`);
      setDirectory({ ...next, entries: [...directory.entries, ...next.entries] });
      setError("");
    } catch (error) {
      setError(String(error));
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <>
      {path === "." && <div className="workspace-root" title={directory.root}><FolderOpen size={16} /><span>{directory.root.split("/").pop() || directory.root}</span></div>}
      <ul className="file-tree">
        {entries.map((entry) => {
          const childPath = path === "." ? entry.name : `${path}/${entry.name}`;
          const isOpen = expanded.has(entry.name);
          return <li key={entry.name}>
            {entry.type === "directory" ? <>
              <div className="tree-entry-row">
                <button className="tree-entry" title={childPath} aria-expanded={isOpen} onClick={() => setExpanded((previous) => {
                  const next = new Set(previous);
                  if (isOpen) next.delete(entry.name); else next.add(entry.name);
                  return next;
                })}>
                  {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                  {isOpen ? <FolderOpen size={15} /> : <Folder size={15} />}<span>{entry.name}</span>
                </button>
                <CopyTextButton className="tree-copy-button" label={`复制相对路径 ${childPath}`} getText={() => childPath} />
              </div>
              {isOpen && <DirectoryTree path={childPath} version={version} sessionId={sessionId} />}
            </> : <div className="tree-entry-row">
              <div className="tree-entry file-entry" title={childPath}><File size={15} /><span>{entry.name}</span>{entry.type === "symlink" && <span>↗</span>}</div>
              <CopyTextButton className="tree-copy-button" label={`复制相对路径 ${childPath}`} getText={() => childPath} />
            </div>}
          </li>;
        })}
        {entries.length === 0 && <li className="empty-directory">空目录</li>}
        {directory.has_more && <li><div className="tree-entry-row"><button className="tree-entry" disabled={loadingMore} onClick={() => { void loadMore(); }}>{loadingMore ? "加载中…" : "加载更多"}</button></div></li>}
      </ul>
    </>
  );
}

export function Workspace({ version, sessionId, collapsed, onCollapsedChange }: { version: number; sessionId?: string; collapsed: boolean; onCollapsedChange: (collapsed: boolean) => void }) {
  const [refresh, setRefresh] = useState(0);
  return <aside className={`workspace-panel ${collapsed ? "collapsed" : ""}`} aria-label="Workspace">
    <button className="workspace-rail-button" aria-label="展开工作区" title="展开工作区" onClick={() => onCollapsedChange(false)}><Folder size={19} /></button>
    <div className="workspace-content">
      <header className="workspace-header"><span>Workspace</span><div className="workspace-header-actions"><button className="icon-button" aria-label="刷新目录" title="刷新目录" onClick={() => setRefresh((value) => value + 1)}><RefreshCw size={14} /></button><button className="icon-button" aria-label="收起工作区" title="收起工作区" onClick={() => onCollapsedChange(true)}><PanelRightClose size={15} /></button></div></header>
      <div className="workspace-files"><DirectoryTree path="." version={version + refresh} sessionId={sessionId} /></div>
      <div className="workspace-footer"><Folder size={13} /> 项目文件</div>
    </div>
  </aside>;
}
