import { useEffect, useState } from "preact/hooks";
import { databaseTab, databaseScope } from "../state/database";
import { useSetting } from "../state/settings";
import { getIdea } from "../state/api";
import type { Experiment } from "../lib/types";
import { allIdeas } from "../state/signals";
import { navigateToIdea } from "../lib/navigate";
import { TablePanel } from "../components/table-panel";
import { DagView } from "./dag-view";
import { LogView } from "./log-view";
import { FilterBar } from "../components/filter-bar";

const ideaQuery = useSetting("databaseIdeaQuery", "");

export function DatabaseView() {
  const tab = databaseTab.value;
  const scope = databaseScope.value;
  const [scopeRows, setScopeRows] = useState<Experiment[] | undefined>();
  const [scopeError, setScopeError] = useState(false);
  useEffect(() => {
    setScopeRows(undefined); setScopeError(false);
    if (!scope || tab !== "experiments") return;
    let cancelled = false;
    getIdea(scope.ideaId, true).then(idea => { if (!cancelled) setScopeRows(idea.experiments ?? []); }).catch(() => { if (!cancelled) setScopeError(true); });
    return () => { cancelled = true; };
  }, [scope, tab]);
  useEffect(() => {
    if (tab !== "graph" || !scope) return;
    const timer = setTimeout(() => {
      document.querySelector(`.subway-station[data-id="${scope.ideaId}"], .subway-dot[data-id="${scope.ideaId}"]`)?.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
    }, 200);
    return () => clearTimeout(timer);
  }, [tab, scope]);
  const ideas = Object.values(allIdeas.value).filter(i => `${i.id} ${i.description} ${i.status}`.toLowerCase().includes(ideaQuery.value.toLowerCase())).sort((a,b) => b.id - a.id);
  return <main class="database-view">
    <div class="database-header"><div><h1>Database</h1><p>Browse experiments, ideas, and their history.</p></div></div>
    <div class="database-tabs" role="tablist" aria-label="Database view">{([ ["experiments", "Experiments"], ["ideas", "Ideas"], ["graph", "Idea graph"], ["log", "Log"] ] as const).map(([id, label]) => <button role="tab" aria-selected={tab === id} class="ui-btn" onClick={() => { databaseTab.value = id; }}>{label}</button>)}</div>
    {scope && <div class="database-scope"><span>{tab === "graph" ? `Idea #${scope.ideaId}` : `Experiment ${scope.label}`}</span><button class="ui-btn" onClick={() => { databaseScope.value = null; }}>Clear selection filter</button></div>}
    {!scope && <FilterBar />}
    {scopeError && <p role="status">Could not load the selected experiment. Showing available records.</p>}
    <section class="database-content" role="tabpanel" aria-label={tab}>
      {tab === "experiments" && <TablePanel ideaId={scope?.ideaId} experimentLabel={scope?.label} records={scopeRows} />}
      {tab === "ideas" && <><input class="database-search" aria-label="Search ideas" placeholder="Search ideas…" value={ideaQuery.value} onInput={e => { ideaQuery.value = e.currentTarget.value; }} /><div class="database-ideas">{ideas.length ? ideas.map(idea => <button class="database-idea" onClick={() => navigateToIdea(idea.id)}><span class="database-idea-id">#{idea.id}</span><span>{idea.description}</span><span class="database-idea-status">{idea.status}</span></button>) : <p>No matching ideas.</p>}</div></>}
      {tab === "graph" && <DagView />}
      {tab === "log" && <LogView />}
    </section>
  </main>;
}
