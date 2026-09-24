# Ticket 002: Inject clock into SQLiteResponseCache for deterministic TTL tests

- **ID**: ticket-002
- **Owner**: unresolved:human
- **Status**: IN_PROGRESS
- **Workflow state**: EDIT
- **Created**: 2026-09-24

## Goal and scope

`SQLiteResponseCache` computes expiry, cooldown and flight-claim timestamps from
the wall clock, so consumers cannot test TTL semantics deterministically. Add an
optional `clock` constructor callable used for all TTL math while `time.sleep`
remains real; callers and defaults are unchanged. Downstream: semcod/search
issue #27 / STARTER-018 (onedev-agent cache-quota test flake under disk load).

Session execution authorized by user request ("kontynuuj" on STARTER-018).

## Acceptance criteria

- [x] AC-01: `SQLiteResponseCache(path, clock=fn)` drives `get`/`put`/`get_or_set`/flight claim/`cooldown`/`cooldown_remaining`/`prune` timestamps; `now=` overrides still win.
- [x] AC-02: Default behavior (no `clock`) is unchanged; full suite passes.

## Tracking boundary

This directory contains the minimal reviewed intent. Optional participant prose
and raw command logs are not required delivery output.
