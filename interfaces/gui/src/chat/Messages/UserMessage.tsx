import { createContext, useContext, useState } from "react";
import { Check, Pencil, X } from "lucide-react";
import { MessagePrimitive, useAuiState } from "@assistant-ui/react";
import { FileAttachmentPart, SignedImage } from "./Attachments";
import { MessageTimestamp } from "./messagePrimitives";

type UserMessageEditContextValue = {
  anchors: ReadonlyMap<number, string>;
  disabled: boolean;
  submit: (anchorId: string, content: string) => Promise<void>;
};

export const UserMessageEditContext = createContext<UserMessageEditContextValue>({
  anchors: new Map(),
  disabled: true,
  submit: async () => undefined,
});

export function UserMessage() {
  const { anchors, disabled, submit } = useContext(UserMessageEditContext);
  const itemIndex = Number(useAuiState((state) => state.message.id));
  const text = useAuiState((state) => state.message.parts
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("\n"));
  const anchorId = anchors.get(itemIndex);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(text);
  const [saving, setSaving] = useState(false);

  async function save() {
    const content = draft.trim();
    if (!anchorId || !content || saving) return;
    setSaving(true);
    try {
      await submit(anchorId, content);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  }

  return <MessagePrimitive.Root className="user-turn">
    <MessageTimestamp className="turn-timestamp" />
    {editing
      ? <div className="user-message user-message-editor"><textarea value={draft} autoFocus onChange={(event) => setDraft(event.target.value)} /></div>
      : <div className="user-message"><MessagePrimitive.Parts components={{ File: FileAttachmentPart, Image: SignedImage }} /></div>}
    {anchorId && <div className="user-message-actions">
      {editing ? <>
        <button type="button" aria-label="取消编辑" title="取消编辑" disabled={saving} onClick={() => { setDraft(text); setEditing(false); }}><X size={13} /></button>
        <button type="button" aria-label="提交编辑" title="提交并创建分支" disabled={saving || !draft.trim()} onClick={() => void save()}><Check size={13} /></button>
      </> : <button type="button" aria-label="编辑消息" title="编辑并创建分支" disabled={disabled} onClick={() => { setDraft(text); setEditing(true); }}><Pencil size={13} /></button>}
    </div>}
  </MessagePrimitive.Root>;
}
