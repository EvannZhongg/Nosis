import { createContext, useContext, useState } from "react";
import { Pencil } from "lucide-react";
import { MessagePrimitive, useAuiState } from "@assistant-ui/react";
import { CopyTextButton } from "../../CopyTextButton";
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
    if (!anchorId || !content || saving || disabled) return;
    setSaving(true);
    try {
      await submit(anchorId, content);
      setEditing(false);
    } catch {
      // The parent surfaces the request error; keep the editor open.
    } finally {
      setSaving(false);
    }
  }

  return <MessagePrimitive.Root className="user-turn">
    <MessageTimestamp className="turn-timestamp" />
    {editing
      ? <div className="user-message user-message-editor">
        <textarea value={draft} autoFocus onChange={(event) => setDraft(event.target.value)} />
        <div className="user-message-editor-actions">
          <button type="button" className="user-edit-cancel" disabled={saving} onClick={() => { setDraft(text); setEditing(false); }}>取消</button>
          <button type="button" className="user-edit-submit" disabled={saving || disabled || !draft.trim()} onClick={() => void save()}>发送</button>
        </div>
      </div>
      : <>
        <div className="user-message"><MessagePrimitive.Parts components={{ File: FileAttachmentPart, Image: SignedImage }} /></div>
        <div className="user-message-meta">
          <CopyTextButton className="user-message-action" label="复制消息" getText={() => text} />
          {anchorId && <button type="button" className="user-message-action" aria-label="编辑消息" title="编辑并创建分支" disabled={disabled} onClick={() => { setDraft(text); setEditing(true); }}><Pencil size={13} /></button>}
        </div>
      </>}
  </MessagePrimitive.Root>;
}
