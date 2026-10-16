# abacus-infra

The orchestration infrastructure repository for [Abacus](https://github.com/dasien/abacus): durable machinery for the control plane. It
**enforces** state preconditions but defines no policy, message meaning or authority (that lives in
abacus-agents). Stdlib Python 3.9+, no dependencies.

| CLI | Purpose |
|---|---|
| `abacus-logbook` | Operating state in SQLite behind one CLI. Current-state tables (workers, dispatches, promises, facts, handovers) and insert-only event tables (messages, reports, verifications, observations, rulings, ruling records and relays), with insert-only enforced by triggers. A dispatch is recorded **before** it is sent; refusals are `PAUSED` (fail closed), `ATTENDED`, `UNKNOWN_WORKER`, `WRONG_REPO`, `DUPLICATE`, `PREREQUISITE`, `UNRELAYED` (a covering ruling was never sent to that worker) and `BAD_CHECK`. `dispatch add --check` carries read-only verification checks, and each dispatch records the prerequisites it waited on and the commits they were verified at (`show dispatches`). A ruling counts as recorded on a bead only if the bead's notes quote it (`NOT_ON_BEAD`); one that names no bead is closed out with `--on none --reason`. `digest` stays under 1 KB and starts with the current time. `rebuild` reconstructs in-flight work from the boards. Old logbooks migrate in place. |
| `abacus-verify` | Treats a worker report as a claim. Checks the bead is closed on its board, the commit is reachable from origin (`ls-remote`, no fetch) and names the bead (`Bead:` trailer), nothing is unpushed, the tree and the bead's worktree are clean, and every check the dispatch carried passes. With `"landing": "draft-pr"` the commit is on the bead's own branch and must be the head of its open PR (`gh pr view`). `--record` writes the verdict. |
| `abacus-session` | Controlled launch path: `claude --bg -n <role>:<repo>` from the owning repo, with the registry's model and mode (`auto` by default), the shared-memory server passed via `--mcp-config`, and `~/.abacus/bin` on `PATH`. Refuses by code: `ROLE`, `ATTENDED`, `BUSY`, `UNSAVED` (checkout or any worktree), `AUTHORITY` (bypass not earned per doctor), `MODEL`, `TRUST`, `OPERATOR`, `LAUNCH`. |
| `abacus-memory` | Shared memory. `harvest` (published records only, read-only against the source) → `ingest` (one write door, idempotent on a content hash, refuses an unmigrated schema) → `consult` / `serve-mcp` (`consult_memory` tool). Outcomes are `hits`, `abstained_below_floor`, `no_rows_matched_filters` and `index_recall`, always with corpus coverage. |
| `checks` (module) | The read-only allowlist for dispatch checks: one program per check, executed without a shell, so `|`, `>` and `$(...)` are literal text. File checks read the **reported commit's** files (via `git archive`, so the checkout lagging origin doesn't matter); git checks run in the repo. |
| `abacus-status` | One screen for the operator, starting with the current time: what needs you, the program, what's in flight, sessions and who started or stopped them. `--line` is the status-line segment, and `--line --wrap` appends it to your own status line. |
| `abacus-lock` | Machine-wide heavy lock (atomic mkdir, stale detection by pid and start time). Contention exits 75 and is never reported as a red gate. |

State lives in `$ABACUS_HOME/state` (default `~/.abacus/state`). Run tests with `make test`.
