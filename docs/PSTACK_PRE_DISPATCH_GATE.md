# PStack Pre-Dispatch Gate

**Purpose:** An old PStack Work item must never gain a Worker lease or cause
an HERDR prompt after its task has left the active lifecycle.

## Authority and decisions

- Ordinary non-PStack Work is unchanged.
- PStack Work must have a matching canonical task in a gateway, logical or
  managed session and the newest lifecycle evidence must be active.
- Missing task, ambiguous canonical copies and unverified lifecycle: fail closed.
- Completed, Expired, Cancelled, Failed: cancel only the never-dispatched
  queued Work before worker assignment, recording an event and audit.
- Blocked: retain queued Work and its history with a
  `pstack_gate_pstack_blocked` reason. No worker is acquired.
- If a Blocked task is explicitly resumed to a newer active lifecycle,
  the scheduler can admit the existing queued Work later.
- A terminal canonical replica prevents accidental resurrection even if
  a newer active mirror appears.

## Race and lease protection

- Scheduler scans queued PStack Work **before** checking idle workers, so a
  terminal item is retired even when all workers are busy.
- SQLite `assign_work` repeats the gate in the same transaction as the worker
  and write lease assignment.
- Execution checks before agent preparation, again after readiness and
  before sending a prompt, and again in the same DB transaction that records
  the dispatch attempt.
- If admission is lost while Work is assigned but no prompt was attempted,
  only that Work is cancelled and its logical lease returned, with audit.
- If an earlier prompt was already attempted, preserve Work/lease uncertainty:
  never conclude remote termination from a PStack status alone.
- Remote prompt submission and SQLite lifecycle checkpointing are separate
  systems; the near-send recheck limits but cannot eliminate a sub-millisecond
  concurrent change after the final DB transaction. Fully atomic external
  dispatch would require HERDR-side fencing tokens.

## Verification

Regression tests cover Completed/Expired/Blocked/Missing, manual-work
isolation, queue-to-assignment race, assignment-to-dispatch race, post-terminal
execution deny, resumable Blocked task and conflicting replicas. An isolated
SQLite backup of Production was used for synthetic active/blocked/closed tasks;
Production DB was not changed.

The feature activates only after production source merge and an
operator-approved HIRDA restart. Do not manually wipe queued PStack history.
