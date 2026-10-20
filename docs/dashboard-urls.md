# Dashboard URLs

The address represents the visible workspace. Navigation uses the History API,
so opening a thread, idea, or log does not reload the application. Refreshing or
sharing the address restores the selection and view configuration against the
latest research data.

## Paths: general to specific

| View | Example |
| --- | --- |
| Overview | `/overview` |
| Conversation | `/messages/threads/42` |
| Specific message | `/messages/threads/42/messages/99` |
| Idea inspector on Overview | `/overview/ideas/123` |
| Experiment inspector in a conversation | `/messages/threads/42/ideas/123/experiments/123.4` |
| Experiment output | `/overview/ideas/123/experiments/123.4/output` |
| Log or script | Same path ending in `/log` or `/script` |
| Idea diff | `/overview/ideas/123/diff` |
| Database | `/database/experiments?scope=123.4` |
| Artifact | `/artifacts/reports/summary.md` |
| Settings | `/settings` or `/settings/agents` |

Opening an inspector preserves the current section. Closing a viewer removes its
suffix; closing the inspector removes its idea/experiment selection. Back and
Forward restore these states.

## Query: graph and filter choices

Defaults are omitted. For example:

```text
/overview?metric=accuracy
/overview?metric=accuracy&graph=scatter:loss,accuracy
/overview?metric=accuracy&graph=line:loss;half;height%3D420
/messages/threads/42?inbox=all&audience=users
```

Overview records the main metric, height and plot controls; additional graphs
record their individual metric/axes, controls, height, width and order. Repeated
`graph` parameters describe multiple additional graphs. `graph=none` removes the
default correlation plot. Each graph has independent settings.

Filters include the named filter or expression, search, tags and tag mode,
hidden statuses, and highlighted ideas/experiments. Conversation URLs also record
the inbox view, search, status, participant and audience. Overview's docked
conversation uses `dock`, `thread` and `message` parameters.

Thread, inspector and section changes create history entries. Graph/filter
adjustments replace the current entry after a short debounce, keeping Back useful.
Legacy hash, idea and compact metric links are accepted and canonicalized.

## Length budget and saved views

**240 characters for the path plus query** is the inline budget. This is a
readability decision, not a browser limit; the deployment's hostname is additional.
Long expressions and many independent graphs cannot remain both fully descriptive
and short. When the budget is exceeded, the application automatically persists an
immutable view configuration and uses a short address such as:

```text
/overview/views/accuracy-3-graphs-0123456789abcdef
```

Snapshots live in the repository's `.the_lab/dashboard_views.json`, so links work
across browsers authenticated to the same dashboard. They contain view settings,
not experiment results or message drafts. Existing links retain their configuration
when subsequent settings change. Identical configurations reuse their identifiers.
Keep this file when moving or backing up the lab. A missing snapshot is reported
in the interface and its URL is preserved. Snapshot APIs require dashboard/admin
access, and payloads are limited to 100 KB.

Plain `/overview` and other explicit paths apply stable defaults independently of
the recipient's browser preferences. The root `/` keeps the existing local Overview
preferences on first entry, then writes its canonical address. Personal typography,
panel widths, drafts, scroll positions, expanded message gaps and read status
remain browser/user state and are not encoded. Links select live data rather than
freezing it at the time the link was copied.

## Implementation and checks

- `dashboard/src/lib/dashboard-url.ts`: pure codec and defaults.
- `dashboard/src/state/navigation.ts`: sole history owner and state restoration.
- `the_lab/routes/dashboard_views.py`: persistent immutable snapshots.
- `node --test tests/dashboard_url.test.mjs`: codec and legacy compatibility.
- `.venv/bin/python -m unittest discover -s tests -p test_dashboard_views.py`:
  persistence, deduplication, access checks and size limits.

The server's existing SPA catch-all serves direct nested routes. Deployments with
an additional frontend reverse proxy must preserve that fallback for non-API paths.
