import { initialRuntimeState, type Incoming, type Outgoing, type RuntimeState } from '@nosis/protocol';
import type { Incoming as HostIncoming, Outgoing as HostOutgoing } from '@nosis/protocol/internal';
// @ts-expect-error Configuration editor schemas are not public exports.
import type { SettingsSnapshot } from '@nosis/protocol';

const initial: RuntimeState = initialRuntimeState();
const open: Outgoing = { type: 'open_session', workspace: '/workspace', session_id: null };
// @ts-expect-error GUI attachment ownership is not a stdio command field.
const attachment: Outgoing = { ...open, attachment_id: 'page' };
// @ts-expect-error First-party settings commands are not public commands.
const settings: Outgoing = { type: 'settings_get' };
// @ts-expect-error GUI notifications are not Bridge process events.
const replaced: Incoming = { type: 'attachment_replaced', phase: 'idle', event_sequence: 1 };

const hostOpen: HostOutgoing = { ...open, attachment_id: 'page' };
const hostReplaced: HostIncoming = { type: 'attachment_replaced', phase: 'idle' };
function inspect(message: Incoming): void {
  void message.replayed;
  // @ts-expect-error Resume cursors are internal to the host.
  void message.resume_after;
  if (message.type === 'assistant_delta') console.log(message.text);
  if (message.type === 'session_ready') {
    const major: 5 = message.protocol_version;
    console.log(major);
  }
}
void [initial, open, attachment, settings, replaced, hostOpen, hostReplaced, inspect];
