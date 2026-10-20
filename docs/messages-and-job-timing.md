# Notifications, message claims, and job timing

## Bounded notifications

Unidentified/dashboard requests do not receive historical agent notifications.
The dashboard uses its inbox, queue, and agent endpoints instead. Identified
agents receive incremental batches: at most eight failure/suggestion/lifecycle
events (a combined 4 KiB budget), plus eight message previews. Events omitted
because of the budget remain pending. Concurrent requests claim each preview or
event once; previews neither mark messages read nor claim ownership of answering.
New agent identities baseline existing historical events instead of replaying them.

List-shaped responses and full thread-history responses are left unchanged and
consume no notification events. The next ordinary dict response, or an explicit
`GET /api/v1/notifications`, can carry the next batch. Failure notifications link
to the specific experiment's log.

## Atomic ownership of one answer

Agents should claim a targeted user question before preparing its direct answer
or starting the requested work. General discussion, clarification, independent
reviews, and questions explicitly seeking multiple viewpoints do not need claims.
Posting messages always remains possible.

```text
POST /api/v1/messages/42/claim
{}
```

The caller is the registered agent from `X-Agent-Id`. An acquired claim returns
`acquired: true` and `claim` containing `message_id`, `owner`, `claimed_at`,
`expires_at`, and `status`. If another agent owns it, the response returns
`acquired: false` and that same ownership record. This is an ordinary coordination
result (HTTP 200), not a failed message post. Retrying your own active acquisition
is idempotent and does not extend the lease. Completed questions return
`acquired: false`, including for the original owner, to avoid duplicate answers.

Additional actions use the same endpoint:

- `{ "action": "renew" }`: explicitly extend your active lease.
- `{ "action": "release" }`: yield; another agent can acquire it immediately.
- `{ "action": "complete", "result_message_id": 99 }`: mark your answer complete;
  the result must be your message in the same thread. The answer link is optional.

Active leases default to one hour; `ttl_seconds` accepts 60–86400 seconds. Ordinary
posts and reads never renew leases. Completed claims remain until explicitly
released, so later readers find the answer. A dashboard/admin operator without an
agent identity can release a claim. Claims do not lock topics, threads, branches,
or experiments. They appear on message records and in the dashboard; a completed
claim can link to its answer.

Ownership is persisted synchronously with atomic JSON replacement in
`.the_lab/message_claims.json`. A thread mutex and an advisory lock on
`.the_lab/message_claims.lock` cover read/check/write, including separate server
processes using the same repository. Keep both files in the lab's normal state
location. No process-local cache decides ownership. The OpenAPI-generated MCP tool
is `claim_message`; the packaged lab instructions explain when agents must use it.
