import { useEffect, useId, useRef, useState } from "react";
import { LoaderCircle, Search } from "lucide-react";
import { get, type SessionSearchMatch, type SessionSearchResults } from "./api";

const SEARCH_RESULT_LIMIT = 50;
const MATCHES_PER_SESSION = 3;

export function SessionSearch({ collapsed, onSelect }: {
  collapsed: boolean;
  onSelect: (match: SessionSearchMatch) => Promise<boolean>;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SessionSearchResults>({ matches: [], has_more: false });
  const [loading, setLoading] = useState(false);
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listId = useId();
  const search = query.trim();

  useEffect(() => {
    if (!open) return;
    inputRef.current?.focus();
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setResults({ matches: [], has_more: false });
    setActiveIndex(0);
    setError("");
    setLoading(Boolean(search));
    if (!search) return;
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({
        q: search,
        limit: String(SEARCH_RESULT_LIMIT),
        per_session_limit: String(MATCHES_PER_SESSION),
      });
      void get<SessionSearchResults>(`/api/sessions/search?${params}`, controller.signal)
        .then((value) => { if (!controller.signal.aborted) setResults(value); })
        .catch((reason) => { if (!controller.signal.aborted) setError(String(reason)); })
        .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, 250);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [open, search]);

  useEffect(() => {
    if (open) document.getElementById(`${listId}-${activeIndex}`)?.scrollIntoView({ block: "nearest" });
  }, [activeIndex, listId, open]);

  async function selectMatch(match: SessionSearchMatch) {
    if (opening) return;
    setOpening(true);
    setError("");
    try {
      if (await onSelect(match)) setOpen(false);
      else setError("无法打开会话，请重新搜索。");
    } catch (reason) {
      setError(String(reason));
    } finally {
      setOpening(false);
    }
  }

  const groups = new Map<string, { match: SessionSearchMatch; index: number }[]>();
  results.matches.forEach((match, index) => {
    const group = groups.get(match.session_id) ?? [];
    group.push({ match, index });
    groups.set(match.session_id, group);
  });

  return <div className="session-search" ref={rootRef}>
    <button ref={triggerRef} type="button" className={`${collapsed ? "sidebar-rail-button" : "sidebar-collapse-button"} session-search-trigger`} aria-label="搜索会话" title="搜索会话" aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen((value) => !value)}><Search size={17} /></button>
    {open && <div className="session-search-popover" role="dialog" aria-label="搜索会话记录">
      <div className="context-popover-title">搜索会话</div>
      <div className="session-search-input">
        {loading || opening ? <LoaderCircle size={15} className="spin" /> : <Search size={15} />}
        <input ref={inputRef} type="text" role="combobox" aria-label="搜索对话记录" aria-autocomplete="list" aria-expanded={results.matches.length > 0} aria-controls={listId} aria-activedescendant={results.matches.length ? `${listId}-${activeIndex}` : undefined} placeholder="搜索对话记录…" maxLength={200} value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => {
          if (event.nativeEvent.isComposing || opening || results.matches.length === 0) return;
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            const direction = event.key === "ArrowDown" ? 1 : -1;
            setActiveIndex((index) => (index + direction + results.matches.length) % results.matches.length);
          } else if (event.key === "Enter") {
            event.preventDefault();
            void selectMatch(results.matches[activeIndex]);
          }
        }} />
      </div>
      {error && <p className="session-search-status error" role="alert">{error}</p>}
      <div className="session-search-results" id={listId} role="listbox" aria-label="搜索结果" aria-busy={loading || opening}>
        {Array.from(groups, ([sessionId, entries]) => {
          const first = entries[0].match;
          return <div className="session-search-group" role="group" aria-label={first.title} key={sessionId}>
            <div className="session-search-heading"><span className="session-search-source" title={`${first.scratch ? "Chat" : first.workspace} / ${first.title}`}><span>{first.scratch ? "Chat" : first.workspace.split(/[\\/]/).pop()}</span><span aria-hidden="true">/</span><strong>{first.title}</strong></span></div>
            {entries.map(({ match, index }) => <button type="button" role="option" id={`${listId}-${index}`} key={match.item_index} tabIndex={-1} aria-selected={index === activeIndex} disabled={opening} className="session-search-result" onMouseEnter={() => setActiveIndex(index)} onClick={() => void selectMatch(match)}>
              {match.timestamp_utc && <span className="session-search-meta"><time dateTime={match.timestamp_utc}>{new Date(match.timestamp_utc).toLocaleString()}</time></span>}
              <span className="session-search-snippet">{match.prefix_omitted && "…"}{match.snippet.slice(0, match.match_start)}<mark>{match.snippet.slice(match.match_start, match.match_end)}</mark>{match.snippet.slice(match.match_end)}{match.suffix_omitted && "…"}</span>
            </button>)}
            {first.has_more_in_session && <p className="session-search-status">仅显示前 {MATCHES_PER_SESSION} 条命中，请输入更具体的关键词。</p>}
          </div>;
        })}
      </div>
      {!error && results.matches.length === 0 && <p className="session-search-status" role="status">{!search ? "输入关键词，搜索所有会话。" : loading ? "正在搜索…" : "未找到相关对话。"}</p>}
      {results.has_more && <p className="session-search-status">已显示前 {SEARCH_RESULT_LIMIT} 条结果，请输入更具体的关键词。</p>}
    </div>}
  </div>;
}
