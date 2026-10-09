# Contributing to cc-cdx-bridge

Thanks for wanting to improve it. Improvements are actively encouraged — small fixes as well as bigger
ones (native Windows support, `mcp` 2.x, newer Codex versions, docs, tests).

> **No support promised.** This is a personal project, provided as is. Issues and pull requests are
> welcome, but there is no promise of support, of a response time, or that a change will be accepted.

## How to contribute

1. **Small fix, typo, extra test, doc improvement:** just open a pull request.
2. **Anything bigger** (a new tool, a new platform, a behaviour change): open an issue first and describe
   the problem and your plan, so we can agree on the direction before you spend time on it.
3. Fork the repository → create a branch from `main` → make the change → run the tests → open a pull
   request that explains *what* changed and *why*.
4. Keep pull requests small and focused: one concern per PR.

## Development rules

- **Run the tests:** `.venv/bin/python test_bridge.py` (16 tests, a fake `codex`, no quota). CI runs the
  same suite on Linux and macOS with Python 3.10 and 3.12 for every pull request — it has to be green.
- **Every behaviour change comes with a test** in `test_bridge.py`. The tests use a fake `codex` shim on
  `PATH` — never the real Codex, never the real `~/.codex/auth.json`, never the real network.
- **Standard library + `mcp<2` only.** No new dependency without a strong reason, discussed in an issue first.
- **State lives on disk, not in the server process** ([`SPEC.md`](SPEC.md) §3): any server instance must be
  able to answer for any job from the files alone. Don't add in-memory state that matters.
- **No shell, ever.** No `shell=True`, no `os.system`, no string-built commands: `argv` goes straight to
  `execve` and prompts travel through files (a test enforces this).
- **Secrets:** never log, print or return the OAuth token; never follow a redirect with an `Authorization`
  header; reduce errors to their type. A change that touches `_fetch_usage`, `AUTH`, `USAGE_URL` or adds
  network calls gets a strict review and may be declined.
- **POSIX only for now** (macOS, Linux, WSL2). Native Windows support is very welcome as a *platform layer*
  (`SPEC.md` §8) — keep the POSIX behaviour unchanged and tested.
- **Keep the spec in sync:** if you change the contract (files, states, tool behaviour), update `SPEC.md`
  and the tool table in `README.md` in the same pull request.
- **Style:** short code, no speculative abstractions, comments only for the *why*, written in English.
  Mark a deliberate simplification that has a known ceiling with a comment naming the ceiling and the
  upgrade path. Match the existing style; no reformat-only changes.
- **Optional live check:** `test_bridge.py --live` runs ONE trivial prompt on the real Codex (it uses a
  little of your quota). If you touch how the `codex exec` command line is built, please run it once and
  say so in the pull request.

## Licensing of contributions

By submitting a pull request you agree that your contribution is licensed under the project's
[MIT license](LICENSE). You keep the copyright on your own work.

## Reporting a security problem

Please do **not** open a public issue for a security problem. Use GitHub's private vulnerability reporting
(the repository's *Security* tab → *Report a vulnerability*) so it can be fixed before it is disclosed.
