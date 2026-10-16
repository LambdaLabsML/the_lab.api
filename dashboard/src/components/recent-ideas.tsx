import { useState } from "preact/hooks";
import { allIdeas, allExperiments } from "../state/signals";
import { navigateToIdea } from "../lib/navigate";
import { relativeTime } from "../lib/format";

export function RecentIdeas() {
  const [limit, setLimit] = useState(5);
  const ideas = Object.values(allIdeas.value).sort((a, b) =>
    (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0) || b.id - a.id);
  const counts = new Map<number, number>();
  for (const experiment of allExperiments.value) counts.set(experiment.idea_id, (counts.get(experiment.idea_id) ?? 0) + 1);
  return <section class="recent-ideas" aria-label="Recent ideas">
    <div class="research-panel-head"><strong>Recent ideas</strong><span>{ideas.length}</span></div>
    {ideas.slice(0, limit).map(idea => <button key={idea.id} class="recent-idea-row" onClick={() => navigateToIdea(idea.id, undefined, { scrollGraph: false })}>
      <span class="recent-idea-id">#{idea.id}</span>
      <span class="recent-idea-title">{idea.description.split("\n")[0]}</span>
      <span class="recent-idea-status">{idea.status}</span>
      <span class="recent-idea-count">{counts.get(idea.id) ?? 0} experiments</span>
      <time dateTime={idea.created_at}>{relativeTime(idea.created_at)}</time>
    </button>)}
    {!ideas.length && <p class="recent-ideas-empty">No ideas yet.</p>}
    {limit < ideas.length && <button class="ui-btn recent-ideas-more" onClick={() => setLimit(value => value + 5)}>Load more</button>}
  </section>;
}
