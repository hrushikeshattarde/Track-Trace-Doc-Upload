"""Production intake service: the durable ledger and the mail-driven loop.

The prototype (run.py, readiness.py) recomputes the world on every run, so anything a run does
not reach is not deferred, it is forgotten: readiness.py caps the load set at --max 150 against a
539-load dashboard, and its attachment de-duplication lives in a Python set scoped to one load in
one run. This package replaces both with state that survives a restart.

  db.py       the ledger (SQLite; plain SQL so Postgres is a port, not a rewrite)
  gmail.py    delegated Gmail access with a history cursor, self-contained
  filters.py  the free filters: size, rate-con filename, pixel geometry
  routing.py  message -> load, in three tiers, then the unresolved list
  ingest.py   Loop A: one pass over everything that arrived since the cursor

Nothing here writes to TransportPro. The mail loop ends by setting a load's next_check_at; the
load loop (not yet built) is what acts on it.
"""

__all__ = ["db", "gmail", "filters", "routing", "ingest"]
