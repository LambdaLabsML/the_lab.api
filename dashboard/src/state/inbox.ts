import { signal } from "@preact/signals";
import { useSetting } from "./settings";

export type InboxView = "for_you" | "mine" | "all" | "saved";
export const threadListWidth = useSetting<number>("threadListWidth", 38);
export const conversationAudience = useSetting<"all" | "me" | "users">("conversationAudience", "all");
export const inboxView = signal<InboxView>("for_you");
export const inboxUnreadCount = signal<number | null>(null);
export const savedConversations = useSetting<number[]>("savedConversations", []);
export const savedConversationFocus = useSetting<Record<string, number>>("savedConversationFocus", {});
export function toggleSavedConversation(id: number, messageId?: number) {
  const removing = savedConversations.value.includes(id);
  savedConversations.value = removing ? savedConversations.value.filter(value => value !== id) : [...savedConversations.value, id];
  const focus = { ...savedConversationFocus.value };
  if (removing) delete focus[String(id)];
  else if (messageId != null) focus[String(id)] = messageId;
  savedConversationFocus.value = focus;
}
