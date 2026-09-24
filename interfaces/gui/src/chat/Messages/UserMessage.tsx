import { MessagePrimitive } from "@assistant-ui/react";
import { FileAttachmentPart, SignedImage } from "./Attachments";
import { MessageTimestamp } from "./messagePrimitives";

export function UserMessage() {
  return <MessagePrimitive.Root className="user-turn">
    <MessageTimestamp className="turn-timestamp" />
    <div className="user-message"><MessagePrimitive.Parts components={{ File: FileAttachmentPart, Image: SignedImage }} /></div>
  </MessagePrimitive.Root>;
}
