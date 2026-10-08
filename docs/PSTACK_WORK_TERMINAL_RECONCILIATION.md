# PStack terminal to Work reconciliation

PStack is the methodology lifecycle authority; HIRDA Work is the execution
and lease authority. Stale queued Work must not remain schedulable after its
canonical PStack task is terminal.

## Implemented safety rules

- On terminal checkpoint, atomically cancel matching queued Work only when
  dispatch attempts are zero, no dispatched timestamp or worker exists, and
  execution state is none or queued.
- On startup, inspect gateway/managed/logical mirrors, choose the highest
  lifecycle revision and fail closed on equally recent conflicting copies.
  Reconcile before Scheduler starts; exclude terminal IDs from requeue.
- Delayed execution status callbacks cannot revive completed tasks; closing
  task tabs and execution environments is still permitted.
- Persist event pstack.execution.terminal_reconciled and audit action
  pstack.work.terminal_reconciled for actual cancellations.
- Dispatched or running Work remains leased unless upstream termination is
  positively proven. An idle/unknown/missing agent pane is NOT sufficient.
- Blocked but resumable tasks are not automatically cancelled.
- Repeated reconciliation is idempotent and preserves all historical records.

## Operational follow-up

Before retiring running Work, independently verify the exact HERDR pane,
agent-session identity and an explicit terminal completion observation.
Use the governed Work and Operations workflow; never blindly release a Worker,
cancel an agent, or restart the service to hide an uncertain lease.

This fix requires merging and an operator-approved HIRDA restart. First run
regressions and certify with a disposable SQLite copy of production data.
