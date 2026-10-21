# Interactive sessions (warm rounds in one admitted attempt)

An `interactive_session` task is an ordinary admitted attempt: claimed under a
resource profile, inputs frozen and staged once, started, leased, completed and
released by the supervisor like any other task. What differs is the handler: it
keeps the loaded model and encoded volume and answers **rounds** until it is
told to close, sits idle for `idle_seconds`, or nears its execution deadline.
No resident-service grant is involved, so case data stays inside the attempt
the admission authority already owns.

```python
async def run_admitted(ctx, params: InteractiveSessionParams):
    state = load_and_encode(ctx.files.primary_path)   # once
    idle_since = time.monotonic()
    while ctx.rounds.execution_remaining_seconds > 60:
        round_ = await ctx.rounds.next_round(wait_s=20)
        if round_ is None:
            if time.monotonic() - idle_since > params.idle_seconds:
                break
            continue
        if round_.kind == "close":
            break
        await ctx.rounds.reply_round(answer(state, round_))  # InteractiveRoundReply
        idle_since = time.monotonic()
    return {"model_specific": {"closed": True}}
```

| Call | Route (attempt token in `X-Attempt-Token`) | Notes |
|---|---|---|
| `next_round(wait_s)` | `GET /tasks/{id}/attempts/{attempt}/rounds/next?wait_s=` | Long-poll, at most 30 s; 204 = none yet. One request, no retry. |
| `reply_round(reply)` | `PUT /tasks/{id}/attempts/{attempt}/rounds/{round_id}/reply` | Retried; the backend keeps the first reply of a round. |

A `propagate` round carries one `InteractiveSeeds` per prompted plane (each axis
once, same dims). A `click` round carries points on one frame. Reply files are
base64 with safe filenames; their format is the consumer's contract, and the
backend validates them before relaying them. A 409 or a retired protocol on
either call ends the lease, exactly as for progress updates.

Sources: [schemas](../src/task_worker_api/schemas/interactive_session.py),
[client](../src/task_worker_api/client.py) (`resource_round_next`,
`resource_round_reply`), [lease](../src/task_worker_api/resource_execution.py),
[tests](../tests/test_interactive_session.py).
