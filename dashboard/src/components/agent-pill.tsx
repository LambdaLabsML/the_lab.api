/**
 * AgentPill — THE shared way to render an agent reference: a neutral outline
 * pill (normal text tone on the dark ground, soft border, bold), same for
 * senders, recipients and the agents' own names. "all" is the broadcast
 * target. Styled via .aa-msg-pill (agent-activity.scss).
 */
export function AgentPill({ id }: { id: string }) {
  return <span class="aa-msg-pill">{id}</span>;
}
