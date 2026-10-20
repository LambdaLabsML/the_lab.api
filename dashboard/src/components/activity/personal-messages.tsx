import { navigateSection } from "../../state/navigation";
import { notificationMessages, isMine, isAddressedToMe } from "../../state/messages";
import { messagesReadByMe } from "../../state/settings";
import { focusMessageId } from "../../state/agent-activity";
import { ResearchIcon } from "../experiment-actions";
import { Eyebrow } from "../ui";

function age(iso: string) {
  const elapsed = Math.max(0, Date.now() - Date.parse(iso));
  if (!Number.isFinite(elapsed)) return "";
  const minutes = Math.floor(elapsed / 60_000);
  return minutes < 1 ? "now" : minutes < 60 ? `${minutes}m` : minutes < 1440 ? `${Math.floor(minutes / 60)}h` : `${Math.floor(minutes / 1440)}d`;
}

/** Same identity and read rules as notifications; a bounded preview, no extra poll. */
export function PersonalMessages() {
  const read = messagesReadByMe.value;
  const recent = notificationMessages.value.filter(m => isMine(m) || isAddressedToMe(m)).sort((a,b) => b.id - a.id).slice(0,6);
  return <section class="personal-messages" aria-label="Your questions and replies">
    <div class="nav-secondary-head"><Eyebrow>Your conversations</Eyebrow></div>
    {!recent.length && <div class="shortlog-idle">Your messages and replies appear here.</div>}
    <div class="personal-messages-list">{recent.map(message => {
      const mine = isMine(message);
      const unread = !mine && !read[String(message.id)];
      const sender = message.from_agent?.replace(/^agent:|^user:/, "") || message.from_role || "Agent";
      return <button key={message.id} class={`personal-message${unread ? " is-unread" : ""}`} title={message.text} onClick={() => {
        focusMessageId.value = message.id;
        // Close the mobile monitor after choosing a conversation.
        window.dispatchEvent(new Event("the-lab:close-monitor"));
        navigateSection("messages");
      }}>
        <ResearchIcon name={mine ? "question" : "reply"} />
        <span class="personal-message-body"><span class="personal-message-meta"><span>{mine ? "You" : `${sender} → you`}</span><time dateTime={message.created_at}>{age(message.created_at)}</time></span><span class="personal-message-preview">{message.tldr || message.text}</span></span>
        {unread && <span class="personal-message-unread" aria-label="Unread reply" />}
      </button>;
    })}</div>
  </section>;
}
