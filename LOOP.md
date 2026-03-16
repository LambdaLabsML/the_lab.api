# The Lab — Agent Loop

## 1. Orient

Read `PROMPT.md` for the research goal, background, and setup.
Check the backlog and the ideas to plan your next steps.

```
GET /backlog          → active ideas, running/pending counts, current branch
GET /ideas            → all ideas with key notes
GET /ideas/<id>       → full idea: experiments, notes, results
```

## 2. Work

```
POST /ideas/new              {parent_ids, description}      → create idea (+ branch)
POST /ideas/<id>/checkout                                   → switch to idea's branch
POST /ideas/<id>/experiments {description, script_content}   → create + write script
POST /experiments/<id>/start                                 → run it
GET  /wait?timeout=N                                        → block until next result
POST /ideas/<id>/note        {text, level}                   → record finding
POST /ideas/<id>/conclude    {conclusion}                    → wrap up, then branch
```

## 3. Repeat

Each `/wait` returns the next finished experiment. Read the result, take notes, decide: run another experiment on this idea, or conclude and branch into a new one.

Scripts must print `{"metrics": {...}}` as last stdout line.
Note levels: `insight` · `milestone` · `observation` (default) · `debug`
