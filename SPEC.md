# cc-cdx-bridge — specification

An asynchronous MCP server between an MCP client (e.g. Claude Code) and the Codex CLI.
This document is written so that a person — or a coding agent — can re-implement or port the bridge
(for example to native Windows, see *Platform layer*) and check the result against the tests.
The reference implementation is `server.py` + `runner.py`; `test_bridge.py` is the executable contract.

## 1. Why

A naive synchronous wrapper (`Popen(["codex", "exec", ...]).communicate(timeout=...)`) has dependable defects:

1. **Hangs.** Without `stdin=DEVNULL` (or a file), `codex exec` inherits the MCP server's stdin pipe and waits
   for EOF forever ("Reading additional input from stdin…"). The same trivial prompt hangs with an open stdin and
   finishes with `</dev/null`.
2. **No visibility.** The result arrives only at the end; no session id is returned.
3. **Nothing reusable after an interruption.** MCP clients restart their servers. The caller re-issues the
   request: duplicates, wasted quota, abandoned work.
4. **No quota information** (5-hour / weekly windows).

**Goals:** the client always sees the state; at the end it gets the result + remaining quota; a human can take
over any run manually in Codex (`codex resume <thread_id>`).

## 2. Environment and constraints

- Python 3.10+, standard library + the `mcp` package, nothing else. The reference implementation uses
  `mcp.server.fastmcp.FastMCP` from **mcp 1.x** (mcp 2.x renamed it `MCPServer` and changed other APIs — pin `mcp<2`).
- Server name: `codex-bridge`. Two files: `server.py` (MCP server) and `runner.py` (detached job runner).
- State directory `jobs/` next to the code (override with `CODEX_BRIDGE_JOBS`, used by the tests).
- Never print or log the contents of `~/.codex/auth.json`; it is read only by the quota function and the token
  never appears in any message or return value.
- `CODEX_BRIDGE_NO_LIMITS=1` disables the quota function entirely (no file read, no network).
- `os.umask(0o077)`: everything written is private to the user.

## 3. Central principle: state lives on disk, not in the MCP process

The client restarts MCP servers. A job must survive that: Codex runs detached, and everything that is read
(status, result) comes from files plus a pid-identity check. **Any server instance answers for any job.**

Job directory `jobs/<job_id>/` (mode 0700), `job_id = YYYYMMDD-HHMMSS-<4 hex>`:

| File | Content | Written by |
|---|---|---|
| `prompt.txt` (0600) | the prompt — becomes Codex's stdin | server, once |
| `meta.json` | job metadata (see below); always written tmp-file + `os.replace` | server |
| `events.jsonl` | Codex stdout (`--json` events), append-only | Codex, via the runner |
| `stderr.log` | Codex stderr | Codex, via the runner |
| `runner.json` | `{"codex_pid": N}` | runner |
| `exit.json` | `{"exit_code", "end_reason": ok\|failed\|cancelled\|timed_out, "cleanup_complete", ["error"]}` — atomic | runner |
| `last_message.txt` | Codex's `-o` output (the final message) | Codex |

`jobs/.lock` — lock file for starters.

`meta.json` fields: `job_id, name, prompt, model, sandbox, cwd, effort, resume_of, hash, pid, pid_start,
started_at, max_runtime_s, thread_id, state, argv`, plus later `exit_code, finished_at, cleanup_complete,
runner_error, cancel_reason, handed_off`.

## 4. Starting a job (`codex_start`)

Parameters: `prompt, model=None, sandbox="read-only" (read-only|workspace-write), cwd=None, name=None,
effort=None, force=False, max_runtime_s=7200`.

1. Validate: non-empty prompt; sandbox in the allowed set; `cwd` exists if given.
2. `hash = sha256(json([prompt, model, sandbox, cwd, effort, resume_thread]))[:16]`.
3. Fetch the quota **before** taking the lock (it may touch the network; under the lock it comes from the cache).
4. Under the exclusive cross-instance lock:
   - **Dedupe:** a `running` job with the same hash ⇒ return it with `deduplicated: true`, start nothing.
   - **Cap:** at most 3 `running` jobs, otherwise refuse and list the active ones.
   - **Quota guard:** if either window ≥ 95 % ⇒ refuse unless `force=True`; ≥ 80 % ⇒ add a warning with the
     percentages. If the quota is unavailable ⇒ do **not** block, add a warning (unless it was disabled on purpose).
   - Create the job directory, write `prompt.txt`, build `argv`:
     `codex exec --json -o <job>/last_message.txt --sandbox <sandbox> --skip-git-repo-check [-m model] [-C cwd]
     [-c model_reasoning_effort=<effort>] -` — for a resume: `… resume <thread_id> -`.
     The flags go **before** the `resume` subcommand. Do **not** pass `--ephemeral` (the session must persist so
     `codex resume` works).
   - Write `meta.json` (state `running`, `pid: null`) **before** spawning, so the cap sees the slot even if the
     spawn is slow.
   - Spawn `python runner.py <job_dir>` detached (own session/process group, stdin/stdout to null, stderr to
     `stderr.log`, **no shell**), then store `pid` and `pid_start` in `meta.json`.
5. Wait up to ~15 s for the `thread.started` event so `thread_id` can be returned (otherwise `null`; it is filled
   in later by status).
6. Return immediately: `job_id, pid, thread_id, state, events_file, resume_command, tail_command, warnings[]`.

### The runner

`runner.py <job_dir>` reads `meta.json`, opens `prompt.txt` **as a file for stdin** (natural EOF, no size limit, no
shell interpolation), `events.jsonl` for stdout and `stderr.log` for stderr, and starts `meta["argv"]` in its **own
process group**. It records `runner.json`, then loops: on SIGTERM (from `codex_cancel`) ⇒ stop the Codex group and
set `end_reason = cancelled`; past the deadline (`max_runtime_s`) ⇒ stop it and set `timed_out`. Stopping = SIGTERM
to the group, wait 5 s, SIGKILL, wait 2 s; `cleanup_complete` = the group is empty afterwards. It writes
`exit.json` atomically with `exit_code`, `end_reason` and `cleanup_complete`. If Codex cannot even be launched
(e.g. not on PATH) it writes `exit_code: 127`, `end_reason: failed`, `error`.

## 5. Events (real `codex exec --json` schema)

```
{"type":"thread.started","thread_id":"…"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id","type":"agent_message","text":"…"}}
{"type":"turn.completed","usage":{"input_tokens","cached_input_tokens","cache_write_input_tokens","output_tokens","reasoning_output_tokens"}}
```

Tasks with tools also emit `item.started` / `item.completed` with other `item.type` values (commands, file edits),
plus `error` and `turn.failed`. The parser must be **tolerant**: ignore non-JSON lines, tolerate missing fields,
summarise unknown types generically (`type` + first ~120 useful characters), take **only complete lines**
(ending in `\n`; the last line may still be in the middle of being written). Do not assume types beyond those above.

## 6. States

`running`, `succeeded`, `failed`, `limit_reached`, `cancelled`, `timed_out`, `lost`.

Derivation (`_refresh`, run on every read; the transition to a terminal state is persisted):

- terminal states are final;
- `thread_id` is taken from `thread.started` as soon as it appears;
- no `exit.json`, no pid yet and started < 30 s ago ⇒ still `running` (spawn in progress);
- no `exit.json` and the runner is **alive** ⇒ `running`;
- no `exit.json` and not alive ⇒ `cancelled` if a `cancel_reason` was recorded, else `lost`;
- with `exit.json` ⇒ classify:
  - `end_reason` is `cancelled` or `timed_out` ⇒ that;
  - `exit_code == 0` and a `turn.completed` event and no `turn.failed` ⇒ `succeeded` (a transient 429 followed by
    `turn.completed` is a success);
  - else `limit_reached` if an `error`/`turn.failed` event has a structured `error.code` matching `limit|quota`, or
    its message / the stderr tail matches the explicit phrasings (`usage limit`, `rate limit`, `insufficient_quota`,
    `too many requests`, `status|http|code … 429`) — **not** on a bare "quota" (a "disk quota exceeded" is something
    else); otherwise `failed`.

**Alive** = the pid exists **and** its start time equals the one recorded at launch. Any mismatch ⇒ uncertain
identity ⇒ treated as not alive, and **no signal is ever sent to that pid** (guards against pid reuse).

`stalled` is a derived flag, not a state: `running` and no new event for more than 300 s. Events are only
completed items, so short silence is normal; the flag is a signal, not a verdict.

## 7. MCP tools

1. `codex_start(...)` — above.
2. `codex_status(job_id)` — `{state, stalled, elapsed_s, idle_s, events, thread_id, last_events (≤ 8 short
   summaries), last_message_so_far (truncated to 1500), usage, exit_code, cleanup_complete, handed_off, error, hint,
   limits, resume_command, events_file}`. `idle_s` comes from the mtime of `events.jsonl`.
3. `codex_wait(job_id, timeout_s=90)` — blocks at most `min(timeout_s, 110)` s (under any MCP client timeout),
   polls every 2 s, reports MCP progress with the last event when a `Context` is available; on timeout returns
   `running` with `hint: "call codex_wait again"` (**not** an error).
4. `codex_result(job_id)` — the final message (from `last_message.txt`; fallback: the last `agent_message`), usage,
   duration, fresh quota, `thread_id`, `resume_command`. If the job is not terminal: status + hint.
5. `codex_cancel(job_id)` — record `cancel_reason`, SIGTERM the runner (which stops the Codex group); if the runner
   does not write `exit.json` within 10 s, SIGKILL it. Confirms whether anything is left (`cleanup_complete`).
6. `codex_resume(job_id_or_thread_id, prompt, …)` — a **new** job continuing the same thread
   (`codex exec resume <thread_id> -`). `model`/`cwd` are inherited from the source job, `sandbox` is not. Accepts a
   thread id the bridge does not know (a session started in the TUI). Refuses if a job on that thread is running or
   the thread is `handed_off` (`force=True` overrides, with a warning).
7. `codex_jobs(limit=20)` — recent jobs: id, name, state, age, thread_id, prompt truncated to 80 characters.
8. `codex_limits()` — `{five_hour:{used_percent, reset_at}, weekly:{used_percent, reset_at}, limit_reached,
   available}`. Source: `GET https://chatgpt.com/backend-api/wham/usage` with `Authorization: Bearer <access_token>`
   and `ChatGPT-Account-Id: <account_id>` from `~/.codex/auth.json` (`tokens.access_token`, `tokens.account_id`);
   response `rate_limit.primary_window` (5 h) and `rate_limit.secondary_window` (weekly), each with `used_percent`
   and `reset_at` (epoch). Cached 60 s; **never follows redirects** (an `Authorization` header must not travel to
   another host); the endpoint is undocumented ⇒ on any error (`401` = expired token, network, other schema)
   return `available: false` + a short reason (type only, no details) — never an exception. `limit_reached` = a window ≥ 95 %.
9. `codex_handoff(job_id, cancel_first=True, release=False)` — manual takeover: if running and `cancel_first`, stop
   it (avoids two writers on one thread); mark the thread `handed_off`; return the exact command
   `codex [-C <cwd>] resume <thread_id>` and explain that the session appears in Codex with its full context.
   `release=True` removes the mark. If the `thread_id` is still unknown, say so explicitly.

## 8. Platform layer (what a native-Windows port must replace)

The reference implementation assumes POSIX. On native Windows `server.py` exits at import with a clear message.
These are the only POSIX-specific concerns; each can be isolated behind a small function:

| Concern | POSIX implementation | What a Windows port needs (guidance — untested) |
|---|---|---|
| Detach the runner | `start_new_session=True` | `creationflags=CREATE_NEW_PROCESS_GROUP \| DETACHED_PROCESS` |
| Kill the whole Codex tree | `os.killpg` TERM → KILL; probe with `killpg(pgid, 0)` | a Job Object (`KILL_ON_JOB_CLOSE`) or `taskkill /PID <pid> /T /F`; "group empty" = no active processes in the job |
| Cancel the runner | SIGTERM handler | no equivalent signal: have the runner poll a `cancel` marker file (it already loops every 0.2 s) |
| Pid identity | `ps -o stat=,lstart= -p <pid>` | creation time via `GetProcessTimes` (ctypes), `psutil.Process(pid).create_time()` (extra dependency) or PowerShell `(Get-Process -Id N).StartTime` |
| Cross-instance lock | `fcntl.flock` on `jobs/.lock` | `msvcrt.locking`, or an `O_CREAT\|O_EXCL` lock file with stale-lock handling |
| Locating `codex` | `Popen(["codex", …])` PATH lookup | `shutil.which("codex")`: npm installs `codex.cmd`, which a bare `Popen(["codex"])` does not resolve; mind `cmd.exe` argument quoting |
| Private files | `umask 077`, 0700/0600 | rely on the user-profile ACLs; keep state under the user's profile; drop the mode assertions in the tests |
| Hints for humans | `tail -f`, `shlex.quote` | PowerShell `Get-Content -Wait`, `subprocess.list2cmdline` |
| Test shim | a `/bin/sh` script named `codex` | a `.cmd` or Python shim; PATH separator `;` |

**WSL2** needs none of this: it is Linux. Keep the state on the Linux filesystem, not under `/mnt/c`.

## 9. Mandatory tests (no quota used — a fake `codex` shim on PATH)

The shim reads stdin until EOF (if stdin is left open it blocks and prints "Reading additional input from stdin…",
exactly like the real Codex), then emits JSONL with configurable `sleep`s, writes the `-o` file and exits with the
requested code. Modes: ok, slow, failure, "usage limit", blocked.

1. `codex_start` returns in < 3 s even when the shim runs for 20 s; `thread_id` is extracted.
2. **stdin regression:** a shim that waits for EOF finishes (if someone removes the stdin handling, the test fails
   or hangs on its timeout). Prompts with quotes/`$(…)`/`;` arrive intact; no `shell=True` anywhere; file modes 0700/0600.
3. `status` while running sees events and `idle_s`; `wait(timeout_s=3)` on a slow job returns `running`, not an error.
4. `result` after the end; `succeeded` / `failed` / `limit_reached` classified correctly.
5. `cancel` kills the **whole** group (no child process left); the runner enforces `max_runtime_s` with nobody polling.
6. Dedupe: a second identical `start` ⇒ same `job_id`, `deduplicated: true`; the cap of 3 refuses the 4th.
7. **Survival:** the state is read correctly by a fresh module instance with no access to the launching process;
   `codex_resume` works from it.
8. Tolerant parser: invalid lines, unknown types, missing fields, an empty file — no exceptions.
9. `limits()` with a simulated response (never hit the real network in tests): 5 h/weekly parsing, `401` ⇒
   `available: false`, cache, quota guard with `force`.
10. The token never follows a redirect and never appears in an error or in the cache.
11. Pid identity: a reused pid ⇒ `lost` and **no signal** (the test process itself is the "reused" pid).
12. Concurrent starters (threads) with the same prompt ⇒ one job; `effort` is part of the dedupe key.
13. Handoff/resume rules: running or `handed_off` ⇒ resume refused; `force`; `release`.
14. Partial last line in `events.jsonl` is neither an error nor lost.
15. `limit_reached` only on explicit phrasings / structured code; a transient 429 followed by success ⇒ `succeeded`.
16. `CODEX_BRIDGE_NO_LIMITS`: no auth read, no network, no warning noise.
17. (`--live`, optional) ONE trivial prompt on the real Codex — only on explicit request.

## 10. Style

Short code, no speculative abstractions, comments only for the *why*. Mark a deliberate simplification that has a
known ceiling with a comment naming the ceiling and the upgrade path.
