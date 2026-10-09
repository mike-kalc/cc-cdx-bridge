# cc-cdx-bridge — run the Codex CLI from Claude Code as an async, resumable job

An [MCP](https://modelcontextprotocol.io) server that lets Claude Code (or any MCP client) hand work to
the OpenAI **Codex CLI** and keep going: jobs run detached in the background, you can watch their
progress, get the result later, continue the same Codex conversation, and — at any moment — take over
by hand in the Codex terminal UI.

**Source:** <https://github.com/mike-kalc/cc-cdx-bridge> · **License:** [MIT](LICENSE) ·
**Maintainer:** Mihai C. ([@mike-kalc](https://github.com/mike-kalc)) ·
**Support:** none promised — see [Support and contributing](#support-and-contributing)

*Naming: the repository is `cc-cdx-bridge` (Claude Code ↔ Codex); the MCP server itself registers as
`codex-bridge` — that is the name you will see in `/mcp` and in the tool names
(`mcp__codex-bridge__codex_start`, …).*

## Why not just shell out to `codex exec`?

A synchronous wrapper has four real problems:

1. **Hangs.** A child `codex exec` that inherits the MCP server's stdin pipe waits for EOF forever
   ("Reading additional input from stdin…") — this froze a real setup for 30 minutes. Here the prompt always
   goes through a file with a clean EOF.
2. **No visibility.** You only see output at the very end. Here you get live status, the last events and
   the message so far.
3. **Nothing survives an interruption.** MCP servers get restarted; a synchronous call is lost and gets
   re-issued (duplicate work, wasted quota). Here the state lives on disk — any server instance answers
   for any job — and identical requests are de-duplicated.
4. **No quota awareness.** Here the 5-hour and weekly ChatGPT quotas are shown and guarded.

## How it works

- `codex_start` writes the prompt to `jobs/<id>/prompt.txt` (mode 0600) and launches a detached
  `runner.py` (own session, no shell). The runner starts `codex exec --json … -` in its own process
  group with the prompt on stdin, streams stdout to `events.jsonl`, enforces `max_runtime_s`
  (TERM → 5 s → KILL on the whole group) and writes `exit.json` atomically.
- Everything the server reports is derived from those files plus the runner's pid identity (pid +
  process start time; a mismatch means *lost*, and no signal is ever sent to a reused pid).
- States: `running`, `succeeded`, `failed`, `limit_reached`, `cancelled`, `timed_out`, `lost`.
  `stalled` is a derived flag (running, but no new event for 300 s).
- Safety rails: identical running jobs are de-duplicated, at most 3 jobs run at once, and starting is
  refused when a quota window is ≥ 95 % (override with `force=True`).
- Full design and rationale: [`SPEC.md`](SPEC.md).

## Requirements

- **macOS, Linux, or Windows through WSL2.** Native Windows is *not* supported (see below).
- **Python 3.10+** and the **`mcp` package, version 1.x** (`requirements.txt` pins `mcp<2` — see *Known limits*).
- **Codex CLI**, installed and logged in with *your own* account:
  `npm install -g @openai/codex`, then `codex login` (see `codex login --help` for other methods).
- **`ps`** on the PATH (standard on macOS and Ubuntu). Minimal containers such as `python:slim` don't
  ship it — `apt-get install procps`.

## Install

### macOS / Linux

```bash
git clone https://github.com/mike-kalc/cc-cdx-bridge.git && cd cc-cdx-bridge   # or unzip the archive and cd into it
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python test_bridge.py        # 16 tests, under a minute, uses no quota (fake `codex` shim)
claude mcp add codex-bridge --scope user -- "$PWD/.venv/bin/python" "$PWD/server.py"
```

Restart Claude Code, check that `codex-bridge` shows as connected in `/mcp`, then ask Claude, for example:

> Start a read-only Codex job named `sec-review` in `~/my-repo` that reviews `src/` for security
> issues. Check on it until it finishes and summarise the result.

On macOS, if `python3` is older than 3.10 (Xcode's is), install a newer one first (`brew install python`).

### Windows

**Recommended: WSL2.** Run everything inside a WSL2 Linux distribution and follow the Linux steps above:

1. `wsl --install` (admin PowerShell), then open the Ubuntu shell.
2. Inside Ubuntu: install Node.js, Python 3.10+ (`python3-venv`), the Codex CLI and run `codex login`.
3. Put the project on the **Linux filesystem** (`~/cc-cdx-bridge`), not under `/mnt/c/…` —
   the bridge relies on POSIX permissions and file locking, which the Windows drive mount handles poorly.
4. If you use Claude Code *inside* WSL2, register it exactly like on Linux. If you use a Windows-side
   Claude Code, you can try launching the server through `wsl.exe`
   (`claude mcp add codex-bridge --scope user -- wsl.exe -e /home/<you>/cc-cdx-bridge/.venv/bin/python /home/<you>/cc-cdx-bridge/server.py`).
   **That Windows-side launch is untested.**

**Native Windows (no WSL): not supported yet.** The server exits with a clear message on purpose. The
POSIX-only parts are small and isolated (process groups, `fcntl` locking, `ps`-based pid identity,
signals); the table in `SPEC.md` → *Platform layer* lists each one with its Windows counterpart.
A good way to port it: give `SPEC.md`, `server.py`, `runner.py` and `test_bridge.py` to your own Claude Code
and ask it to implement the platform layer — then run the test suite on your Windows machine.

## Tools

| Tool | What it does |
|---|---|
| `codex_start(prompt, model?, sandbox="read-only", cwd?, name?, effort?, force=False, max_runtime_s=7200)` | start a detached job; returns at once with `job_id`, `thread_id`, `resume_command`, `tail_command`, `warnings` |
| `codex_status(job_id)` | state, `stalled`, elapsed/idle seconds, last 8 events, message so far, usage, quota, `cleanup_complete`, `handed_off` |
| `codex_wait(job_id, timeout_s=90)` | blocks up to min(timeout_s, 110) s; on timeout returns `running` + a hint (not an error) — just call it again |
| `codex_result(job_id)` | the full final message, usage, duration, fresh quota, `thread_id`, `resume_command` |
| `codex_cancel(job_id)` | stops the job and its whole process group; `cleanup_complete` says whether anything is left |
| `codex_resume(job_id_or_thread_id, prompt, …)` | new job on the **same** Codex thread (`codex exec resume`); refuses while that thread is running or handed off (`force=True` overrides, with a warning) |
| `codex_jobs(limit=20)` | recent jobs: state, age, `thread_id`, prompt truncated to 80 characters |
| `codex_limits()` | ChatGPT 5-hour and weekly quota (cached 60 s); `available: false` + reason instead of an exception |
| `codex_handoff(job_id, cancel_first=True, release=False)` | stop the job, mark the thread as handed to you, print `codex [-C cwd] resume <thread_id>`; `release=True` removes the mark |

### Taking over by hand

```text
codex_handoff(job_id)          → run the printed `codex resume <thread_id>` in a terminal
                                  (the session shows the full context: prompt, tools used, replies)
codex_handoff(job_id, release=True)   when you are done, so the bridge may resume the thread again
```

## Security and privacy notes — read these before sharing it

- **Whoever can call these tools can run Codex with *your* login.** Treat access to the MCP server like
  access to a shell-adjacent capability. The default `sandbox="read-only"` limits what Codex may do;
  `workspace-write` lets it edit files under `cwd` — use it deliberately.
- **No shell is ever spawned.** The prompt travels through a file; `argv` goes straight to `execve`.
- **Prompts and results are stored in `jobs/<id>/`** (directory 0700, files 0600). There is **no retention
  policy** — delete old job directories yourself. `jobs/` is git-ignored.
- **`codex_limits` reads your Codex OAuth access token** from `~/.codex/auth.json` and calls an
  **undocumented** ChatGPT usage endpoint (`https://chatgpt.com/backend-api/wham/usage`). Redirects are
  never followed, the token is never logged or returned, and errors are reduced to their type. The endpoint
  may change or disappear at any time — the bridge then just runs without the quota guard.
  **To turn this off completely** (no read of `auth.json`, no network call), set `CODEX_BRIDGE_NO_LIMITS=1`:
  `claude mcp add codex-bridge --scope user -e CODEX_BRIDGE_NO_LIMITS=1 -- "$PWD/.venv/bin/python" "$PWD/server.py"`

## Tests

```bash
.venv/bin/python test_bridge.py          # 16 tests, fake `codex` on PATH, no quota, no real auth file
.venv/bin/python test_bridge.py --live   # ONE trivial prompt on the REAL Codex (consumes a little quota)
```

The suite covers: instant return from `codex_start`, the stdin-EOF regression, status/short waits,
result classification, killing the whole process group, de-duplication and the 3-job cap, survival of a
server restart, a tolerant event parser, quota parsing and guard, no-redirect for the token, pid-identity
checks, cross-instance locking, handoff/resume rules, partial-line handling, and the quota opt-out.

## Known limits

- No isolation against Codex descendants that change their own process group or session: after a kill the
  bridge only reports `cleanup_complete` (whether the Codex group is empty). On macOS a transient `EPERM`
  on re-parented members is treated as "still there".
- `codex exec` flags used (`--json -o --sandbox -m -C -c --skip-git-repo-check`, subcommand `resume`) were
  verified against Codex CLI 0.155.1. If a newer Codex changes them, check `codex exec --help`.
- Event schema parsing is deliberately tolerant (unknown event types are summarised, bad lines ignored).
- **Pinned to `mcp` 1.x.** The server uses `mcp.server.fastmcp.FastMCP`; `mcp` 2.x renamed it to `MCPServer` and
  changed other APIs, so an unpinned `pip install mcp` breaks the import. Porting to 2.x has not been done.
- State is never cleaned up: remove old `jobs/<id>/` directories yourself.

## Compatibility

The 16-test suite runs on every push and pull request through GitHub Actions
(`.github/workflows/test.yml`: Ubuntu and macOS, Python 3.10 and 3.12). It was also run by hand on a
clean install from `requirements.txt`:

| Platform | Python | mcp | Result |
|---|---|---|---|
| macOS (Apple Silicon) | 3.14 | 1.30.0 | 16/16 pass |
| Linux (Debian, Docker, arm64) | 3.10 (declared minimum) | 1.30.0 | 16/16 pass |
| Linux (Debian, Docker, arm64) | 3.12 | 1.30.0 | 16/16 pass |

Also checked:
- A real MCP handshake over stdio (`initialize` → `tools/list` → `tools/call`): 9 tools are listed.
- `test_bridge.py --live` against the **real Codex CLI 0.155.1** (macOS, ChatGPT login): one trivial prompt
  finished as `succeeded` in about 10 seconds, and the quota endpoint answered.

**Not run — please don't assume these work:**
- Native Windows (unsupported on purpose) and a real WSL2 install (it is Linux, so it should behave like the
  Docker runs above, but it has not been run on actual WSL2).
- Linux on x86-64 (the runs above are all arm64).
- `--live` on Linux/WSL2 or with other Codex versions — run it once yourself after installing.

## Support and contributing

**No support is promised.** This is a personal project, provided as is (see the warranty disclaimer in the
[license](LICENSE)). There is no promise of support, response times, bug fixes or ongoing maintenance;
issues and pull requests may be answered slowly or not at all.

**Improvements are welcome and encouraged** — fixes, tests, docs, newer Codex versions, `mcp` 2.x, and
especially native Windows support (the plan is in `SPEC.md` §8). How to proceed:

- small fix or doc improvement → open a pull request;
- anything bigger → open an issue first so we can agree on the direction;
- fork → branch → change → run `python test_bridge.py` → pull request. CI runs the suite for you.

The development rules (tests for every behaviour change, standard library + `mcp<2` only, no shell, how
secrets are handled, how to report a security problem) are in [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

[MIT](LICENSE) — Copyright (c) 2026 Mihai C. Contributions are accepted under the same license.
