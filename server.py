#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mihai C.
"""MCP server codex-bridge: Codex CLI run asynchronously, with all state on disk.

A job = a detached `runner.py <job_dir>` (own session) that starts `codex exec` in its own process
group with the prompt from prompt.txt on stdin, stdout -> events.jsonl, and writes exit.json
atomically at the end (exit_code + end_reason). Claude Code restarts MCP servers, so NOTHING
essential lives in the server process: any instance answers for any job from files alone
+ the pid identity (pid + start time). A human can take over any session with
`codex resume <thread_id>`. Specification: SPEC.md; decisions: README.md.
"""
import os
import sys

if os.name == "nt":  # before `import fcntl`: a clear message instead of a ModuleNotFoundError
    sys.exit("codex-bridge needs POSIX (macOS, Linux or WSL2); native Windows is not supported "
             "— see README.md")

import asyncio
import contextlib
import fcntl
import hashlib
import json
import re
import secrets
import shlex
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

os.umask(0o077)  # everything we write (jobs/, meta.json, prompt.txt) is readable by the user only
mcp = FastMCP("codex-bridge")
HERE = Path(__file__).resolve().parent
RUNNER = HERE / "runner.py"
# CODEX_BRIDGE_JOBS exists only so the tests can work in a temporary directory.
JOBS = Path(os.environ.get("CODEX_BRIDGE_JOBS") or HERE / "jobs")
AUTH = Path.home() / ".codex/auth.json"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
MAX_RUNNING = 3
STALL_AFTER_S = 300
THREAD_WAIT_S = 15
TERMINAL = {"succeeded", "failed", "limit_reached", "cancelled", "timed_out", "lost"}
# free text: only explicit quota/rate phrasings (not a bare "quota": "disk quota exceeded" is something else)
LIMIT_RE = re.compile(r"usage[ _]limit|rate[ _]limit|insufficient_quota|too many requests|"
                      r"(?:status|http|code)\W{0,3}429\b", re.I)
CODE_RE = re.compile(r"limit|quota", re.I)  # only on the structured code (error.code)
FAILOVER_HINT = ("Codex quota exhausted — wait for the reset (see codex_limits) "
                 "or fall back to another model/provider")
# Only to reap (poll) runners launched by THIS instance; the real state is on disk.
_PROCS: dict[str, subprocess.Popen] = {}
_LIMITS: dict = {"at": 0.0, "data": None}


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))


def _trunc(s, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[:n] + f"…[+{len(s) - n}]"


def _tail(path: Path, n: int = 2000) -> str:
    try:
        return path.read_text(errors="replace")[-n:]
    except OSError:
        return ""


# ---------- state on disk ----------

def _meta(d: Path) -> dict:
    return json.loads((d / "meta.json").read_text())


def _save(d: Path, meta: dict) -> None:
    # atomic write: a half-written meta.json would break codex_jobs for every job
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    os.replace(tmp, d / "meta.json")


def _json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _events(d: Path) -> list[dict]:
    """Tolerant JSONL: only COMPLETE lines (ending in \\n) — the last line, still being written, is
    read on the next poll; non-JSON lines (Codex also writes plain text) are ignored."""
    try:
        text = (d / "events.jsonl").read_text(errors="replace")
    except OSError:
        return []
    out = []
    for line in text[:text.rfind("\n") + 1].splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def _thread_id(events: list[dict]) -> str | None:
    return next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)


def _errmsg(e: dict) -> str:
    err = e.get("error")
    return str(e.get("message") or (err.get("message") if isinstance(err, dict) else err) or "")


def _errcode(e: dict) -> str:
    err = e.get("error")
    return str((err.get("code") if isinstance(err, dict) else None) or e.get("code") or "")


def _summary(e: dict) -> str:
    """One line per event; unknown types are summarised generically (type + ~120 characters)."""
    t = str(e.get("type", "?"))
    item = e.get("item")
    if t == "thread.started":
        return f"{t} {e.get('thread_id')}"
    if t.startswith("item.") and isinstance(item, dict):
        body = item.get("text") or item.get("command") or {k: v for k, v in item.items() if k not in ("id", "type")}
        body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        return f"{t} [{item.get('type', '?')}] {_trunc(' '.join(body.split()), 120)}"
    if t == "turn.completed":
        u = e.get("usage") if isinstance(e.get("usage"), dict) else {}
        return f"{t} in={u.get('input_tokens')} out={u.get('output_tokens')}"
    if t in ("error", "turn.failed"):
        return f"{t}: {_trunc(' '.join(_errmsg(e).split()), 120)}"
    rest = {k: v for k, v in e.items() if k != "type"}
    return f"{t} {_trunc(' '.join(json.dumps(rest, ensure_ascii=False).split()), 120)}" if rest else t


def _messages(events: list[dict]) -> list[str]:
    return [e["item"]["text"] for e in events
            if e.get("type") == "item.completed" and isinstance(e.get("item"), dict)
            and e["item"].get("type") == "agent_message" and isinstance(e["item"].get("text"), str)]


# ---------- processes ----------

def _lstart(pid: int) -> str:
    """Process start time (ps): the pid's identity against reuse. "" = nonexistent/zombie."""
    try:
        out = subprocess.run(["ps", "-o", "stat=,lstart=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout.split(None, 1)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out[1].strip() if len(out) == 2 and not out[0].startswith("Z") else ""


def _reap(job_id: str) -> None:
    # without a poll, a finished runner stays a zombie and would look alive
    p = _PROCS.get(job_id)
    if p is not None:
        p.poll()


def _alive(meta: dict) -> bool:
    """Alive ONLY if the pid exists and has the same start time as at launch;
    any mismatch = uncertain identity = we never signal that pid."""
    if not meta.get("pid") or not meta.get("pid_start"):
        return False
    _reap(meta["job_id"])
    s = _lstart(meta["pid"])
    return bool(s) and s == meta["pid_start"]


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # macOS: transient EPERM on a re-parented member, not yet reaped


def _codex_group_clean(d: Path) -> bool:
    r = _json(d / "runner.json")
    return not (r and r.get("codex_pid") and _group_exists(r["codex_pid"]))


def _classify(d: Path, events: list[dict], end: dict) -> str:
    if end.get("end_reason") in ("cancelled", "timed_out"):
        return end["end_reason"]
    kinds = {e.get("type") for e in events}
    if end.get("exit_code") == 0 and "turn.completed" in kinds and "turn.failed" not in kinds:
        return "succeeded"  # a transient 429 followed by turn.completed is a success, not limit_reached
    errs = [e for e in events if e.get("type") in ("error", "turn.failed")]
    codes = " ".join(_errcode(e) for e in errs)
    text = " ".join(_errmsg(e) for e in errs) + " " + _tail(d / "stderr.log")
    return "limit_reached" if CODE_RE.search(codes) or LIMIT_RE.search(text) else "failed"


def _refresh(d: Path, meta: dict) -> dict:
    """Derive the state from disk + pid identity; persist the transition to a terminal state."""
    if meta["state"] in TERMINAL:
        return meta
    if not meta.get("thread_id"):
        tid = _thread_id(_events(d))
        if tid:
            meta["thread_id"] = tid
            _save(d, meta)
    end = _json(d / "exit.json")
    if end is None and meta.get("pid") is None and time.time() - meta["started_at"] < 30:
        return meta  # slot reserved under the lock, spawn in progress
    if end is None and _alive(meta):
        return meta
    end = end or _json(d / "exit.json")  # the runner may write exit.json and die between the two checks
    if end is None:
        state = "cancelled" if meta.get("cancel_reason") else "lost"
        meta["cleanup_complete"] = _codex_group_clean(d) if meta.get("cancel_reason") else None
    else:
        state = _classify(d, _events(d), end)
        meta["exit_code"] = end.get("exit_code")
        meta["cleanup_complete"] = bool(end.get("cleanup_complete")) and _codex_group_clean(d)
        if end.get("error"):
            meta["runner_error"] = end["error"]
    meta.update(state=state, finished_at=time.time())
    _save(d, meta)
    return meta


def _cancel(d: Path, meta: dict, why: str) -> dict:
    """Reason on disk first (if the runner dies meanwhile the state stays cancelled, not lost),
    then SIGTERM to the runner, which stops the Codex group (TERM, 5 s, KILL) and writes exit.json."""
    meta["cancel_reason"] = why
    _save(d, meta)
    if _alive(meta):  # uncertain identity or dead => we send no signal at all
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(meta["pid"], signal.SIGTERM)
        for _ in range(100):
            time.sleep(0.1)
            if _json(d / "exit.json") is not None:
                break
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(meta["pid"], signal.SIGKILL)  # stuck runner (should not happen); cleanup_complete tells the rest
    return _refresh(d, meta)


# ---------- job lookup ----------

def _all_jobs() -> list[dict]:
    """All jobs, newest first, with refreshed state (cheap for the terminal ones)."""
    out = []
    for d in sorted(JOBS.iterdir(), reverse=True) if JOBS.is_dir() else []:
        if (d / "meta.json").is_file():
            out.append(_refresh(d, _meta(d)))
    return out


def _find(key: str) -> tuple[Path, dict] | None:
    d = JOBS / key
    if "/" not in key and not key.startswith(".") and (d / "meta.json").is_file():
        return d, _refresh(d, _meta(d))
    for m in _all_jobs():  # may be a thread_id (codex_resume / codex_handoff)
        if m.get("thread_id") == key:
            return JOBS / m["job_id"], m
    return None


def _job(key: str) -> tuple[Path, dict]:
    found = _find(key)
    if found is None:
        raise ValueError(f"unknown job: {key} (see codex_jobs)")
    return found


@contextlib.contextmanager
def _locked():
    """One starter at a time, across all server instances (dedupe + cap + slot reservation)."""
    JOBS.mkdir(parents=True, exist_ok=True)
    with open(JOBS / ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


# ---------- quota ----------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None  # a redirect would carry Authorization to another host; urllib raises the 3xx HTTPError instead


_OPENER = urllib.request.build_opener(_NoRedirect)


def _fetch_usage() -> dict:
    """The raw GET, kept separate so tests can simulate it without a network. The token never appears in any message."""
    t = json.loads(AUTH.read_text())["tokens"]
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {t['access_token']}", "ChatGPT-Account-Id": t["account_id"],
        "User-Agent": "codex-bridge"})
    with _OPENER.open(req, timeout=10) as r:
        return json.load(r)


def _limits(fresh: bool = False) -> dict:
    if os.environ.get("CODEX_BRIDGE_NO_LIMITS"):  # opt-out: never read auth.json, never touch the network
        return {"available": False, "reason": "disabled (CODEX_BRIDGE_NO_LIMITS)"}
    if not fresh and _LIMITS["data"] and time.time() - _LIMITS["at"] < 60:
        return _LIMITS["data"]
    try:
        rl = _fetch_usage().get("rate_limit") or {}
        out = {"available": True}
        for name, key in (("five_hour", "primary_window"), ("weekly", "secondary_window")):
            w = rl.get(key) if isinstance(rl.get(key), dict) else {}
            pct, reset = w.get("used_percent"), w.get("reset_at")
            out[name] = {"used_percent": pct if isinstance(pct, (int, float)) else None,
                         "reset_at": _iso(reset) if isinstance(reset, (int, float)) else None}
        used = [out[k]["used_percent"] for k in ("five_hour", "weekly") if out[k]["used_percent"] is not None]
        if not used:
            raise ValueError("response without quota windows")
        out["limit_reached"] = max(used) >= 95  # the threshold at which codex_start refuses without force
    except urllib.error.HTTPError as e:
        e.close()  # an HTTPError is also a file-like response; left open it raises a ResourceWarning
        why = "token expired (run any `codex` command to refresh it)" if e.code == 401 else f"HTTP {e.code}"
        out = {"available": False, "reason": why}
    except Exception as e:  # missing file, network, different schema — undocumented endpoint; type only, no details
        out = {"available": False, "reason": type(e).__name__}
    _LIMITS.update(at=time.time(), data=out)
    return out


# ---------- launching ----------

def _resume_cmd(meta: dict) -> str | None:
    if not meta.get("thread_id"):
        return None
    cd = f"-C {shlex.quote(meta['cwd'])} " if meta.get("cwd") else ""
    return f"codex {cd}resume {meta['thread_id']}"


def _start_reply(meta: dict) -> dict:
    d = JOBS / meta["job_id"]
    return {"job_id": meta["job_id"], "pid": meta.get("pid"), "thread_id": meta.get("thread_id"),
            "state": meta["state"], "events_file": str(d / "events.jsonl"),
            "resume_command": _resume_cmd(meta),
            "tail_command": f"tail -f {shlex.quote(str(d / 'events.jsonl'))}", "warnings": []}


def _launch(prompt, model, sandbox, cwd, name, effort, force, max_runtime_s, resume_thread=None) -> dict:
    if not prompt or not prompt.strip():
        raise ValueError("empty prompt")
    if sandbox not in ("read-only", "workspace-write"):
        raise ValueError("sandbox: read-only | workspace-write")
    if cwd and not os.path.isdir(cwd):
        raise ValueError(f"cwd does not exist: {cwd}")
    key = hashlib.sha256(json.dumps([prompt, model, sandbox, cwd, effort, resume_thread]).encode()).hexdigest()[:16]
    lim = _limits()  # before the lock: may hit the network; under the lock it comes from the cache
    with _locked():
        running = [m for m in _all_jobs() if m["state"] == "running"]
        for m in running:
            if m.get("hash") == key:
                return {**_start_reply(m), "deduplicated": True}
        if len(running) >= MAX_RUNNING:
            active = ", ".join(f"{m['job_id']} ({m.get('name') or _trunc(m['prompt'], 40)})" for m in running)
            raise RuntimeError(f"cap of {MAX_RUNNING} running jobs reached; active: {active} — "
                               "wait (codex_wait) or cancel (codex_cancel)")
        warnings = []
        if lim.get("available"):
            pct = {k: lim[k]["used_percent"] or 0 for k in ("five_hour", "weekly")}
            if max(pct.values()) >= 95 and not force:
                raise RuntimeError(f"Codex quota: 5h {pct['five_hour']}%, weekly {pct['weekly']}% — "
                                   f"refusing (use force=True to override). {FAILOVER_HINT}")
            if max(pct.values()) >= 80:
                warnings.append(f"Codex quota: 5h {pct['five_hour']}%, weekly {pct['weekly']}%")
        elif not os.environ.get("CODEX_BRIDGE_NO_LIMITS"):
            warnings.append(f"quota unavailable ({lim.get('reason')}) — starting without the guard")

        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        d = JOBS / job_id
        d.mkdir(mode=0o700)
        (d / "prompt.txt").write_text(prompt)  # umask 077 => 0600; Codex's stdin (natural EOF)
        argv = ["codex", "exec", "--json", "-o", str(d / "last_message.txt"),
                "--sandbox", sandbox, "--skip-git-repo-check"]
        if model:
            argv += ["-m", model]
        if cwd:
            argv += ["-C", cwd]
        if effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        # flags before the `resume` subcommand (clap accepts them there for sure); `-` = prompt on stdin
        argv += ["resume", resume_thread, "-"] if resume_thread else ["-"]
        meta = {"job_id": job_id, "name": name, "prompt": prompt, "model": model, "sandbox": sandbox,
                "cwd": cwd, "effort": effort, "resume_of": resume_thread, "hash": key, "pid": None,
                "pid_start": None, "started_at": time.time(), "max_runtime_s": max_runtime_s,
                "thread_id": None, "state": "running", "argv": argv}
        _save(d, meta)  # the slot is reserved before the spawn: the cap sees it even if the spawn is slow
        # no shell: argv/paths travel only as arguments; the runner makes its own session
        with open(d / "stderr.log", "ab") as err:
            p = subprocess.Popen([sys.executable, str(RUNNER), str(d)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=err, start_new_session=True)
        _PROCS[job_id] = p
        meta.update(pid=p.pid, pid_start=_lstart(p.pid))
        _save(d, meta)
    deadline = time.time() + THREAD_WAIT_S
    while time.time() < deadline and not _thread_id(_events(d)) and not (d / "exit.json").exists():
        time.sleep(0.2)
    return {**_start_reply(_refresh(d, meta)), "warnings": warnings}


def _status(d: Path, meta: dict) -> dict:
    meta = _refresh(d, meta)
    events = _events(d)
    now = time.time()
    try:
        st = (d / "events.jsonl").stat()
        last_at = st.st_mtime if st.st_size else meta["started_at"]
    except OSError:
        last_at = meta["started_at"]
    end = meta.get("finished_at") or now
    running = meta["state"] == "running"
    msgs = _messages(events)
    errs = [m for m in (_errmsg(e) for e in events if e.get("type") in ("error", "turn.failed")) if m]
    error = None
    if meta["state"] not in ("running", "succeeded", "cancelled"):
        error = _trunc(" | ".join(errs) or meta.get("runner_error") or
                       " ".join(_tail(d / "stderr.log", 600).split()), 400) or None
    hint = {"limit_reached": FAILOVER_HINT,
            "lost": "process vanished without exit.json (reboot / external kill / reused pid); "
                    "the context lives in the Codex session: see resume_command"
            }.get(meta["state"])
    return {"job_id": meta["job_id"], "name": meta.get("name"), "state": meta["state"],
            "stalled": running and now - last_at > STALL_AFTER_S,
            "elapsed_s": round(end - meta["started_at"], 1), "idle_s": round(now - last_at, 1),
            "events": len(events), "thread_id": meta.get("thread_id"),
            "started_at": _iso(meta["started_at"]), "last_event_at": _iso(last_at) if events else None,
            "last_events": [_summary(e) for e in events[-8:]],
            "last_message_so_far": _trunc(msgs[-1], 1500) if msgs else None,
            "usage": next((e.get("usage") for e in reversed(events) if e.get("type") == "turn.completed"), None),
            "exit_code": meta.get("exit_code"), "cleanup_complete": meta.get("cleanup_complete"),
            "handed_off": bool(meta.get("handed_off")), "error": error, "hint": hint, "limits": _limits(),
            "resume_command": _resume_cmd(meta), "events_file": str(d / "events.jsonl")}


def _mark_handed_off(thread_id: str, flag: bool) -> None:
    for m in _all_jobs():
        if m.get("thread_id") == thread_id and bool(m.get("handed_off")) != flag:
            m["handed_off"] = flag
            _save(JOBS / m["job_id"], m)


# ---------- MCP tools ----------

@mcp.tool()
def codex_start(prompt: str, model: str | None = None, sandbox: str = "read-only", cwd: str | None = None,
                name: str | None = None, effort: str | None = None, force: bool = False,
                max_runtime_s: int = 7200) -> dict:
    """Start a detached Codex job (`codex exec`, prompt on stdin) and return immediately with
    job_id + thread_id. Dedupe on (prompt, model, sandbox, cwd, effort) while an identical job
    runs; max 3 running jobs; quota guard (>= 95% refuses unless force=True). Follow it with
    codex_status / codex_wait, get the outcome with codex_result."""
    return _launch(prompt, model, sandbox, cwd, name, effort, force, max_runtime_s)


@mcp.tool()
def codex_status(job_id: str) -> dict:
    """State of a job, from disk: state, stalled (>300 s without events), elapsed/idle, last
    events, the message so far, usage, quota, resume_command."""
    return _status(*_job(job_id))


@mcp.tool()
async def codex_wait(job_id: str, timeout_s: int = 90, ctx: Context | None = None) -> dict:
    """Wait (at most min(timeout_s, 110) s, under the MCP client's timeout) for a terminal state; on
    timeout returns state=running + a hint (NOT an error) — call it again."""
    d, meta = _job(job_id)
    deadline = time.time() + min(timeout_s, 110)
    while True:
        st = await asyncio.to_thread(_status, d, meta)  # disk I/O (+ ps, quota) off the event loop
        if st["state"] != "running" or time.time() >= deadline:
            break
        if ctx is not None:
            with contextlib.suppress(Exception):  # progress is optional, the client may not support it
                await ctx.report_progress(st["elapsed_s"], None, (st["last_events"] or ["(no events)"])[-1])
        await asyncio.sleep(2)
    if st["state"] == "running":
        st["hint"] = "call codex_wait again"
    return st


@mcp.tool()
def codex_result(job_id: str) -> dict:
    """Final outcome: the full message, usage, duration, fresh quota, thread_id, resume_command.
    If the job is not terminal: status + a hint."""
    d, meta = _job(job_id)
    st = _status(d, meta)
    if st["state"] == "running":
        return {**st, "hint": "job still running — codex_wait"}
    try:
        text = (d / "last_message.txt").read_text().strip()
    except OSError:
        text = ""
    msgs = _messages(_events(d))
    return {**st, "result": text or (msgs[-1] if msgs else None), "duration_s": st["elapsed_s"],
            "limits": _limits(fresh=True)}


@mcp.tool()
def codex_cancel(job_id: str) -> dict:
    """Stop the job: SIGTERM to the runner, which sends SIGTERM to the Codex group and SIGKILL after
    5 s; cleanup_complete=True means no process of the Codex group is left."""
    d, meta = _job(job_id)
    if meta["state"] != "running":
        return {"job_id": meta["job_id"], "state": meta["state"], "note": "job was not running"}
    meta = _cancel(d, meta, "codex_cancel")
    return {"job_id": meta["job_id"], "state": meta["state"], "cleanup_complete": meta.get("cleanup_complete"),
            "thread_id": meta.get("thread_id"), "resume_command": _resume_cmd(meta)}


@mcp.tool()
def codex_resume(job_id_or_thread_id: str, prompt: str, model: str | None = None, sandbox: str = "read-only",
                 cwd: str | None = None, name: str | None = None, effort: str | None = None,
                 force: bool = False, max_runtime_s: int = 7200) -> dict:
    """New job that continues the same Codex thread (`codex exec resume <thread_id>`, prompt on
    stdin); model/cwd are inherited from the source job when omitted. Refuses if a job on the same
    thread is still running or the thread was handed to the human (codex_handoff) — force=True
    overrides, with a warning. Also accepts a thread_id the bridge does not know."""
    found = _find(job_id_or_thread_id)
    if found is None:
        thread_id = job_id_or_thread_id  # thread started outside the bridge (TUI); we continue it anyway
    else:
        _, meta = found
        if not meta.get("thread_id"):
            raise ValueError("thread_id unknown for this job (Codex did not emit thread.started)")
        thread_id, model, cwd = meta["thread_id"], model or meta.get("model"), cwd or meta.get("cwd")
    busy = [m for m in _all_jobs() if m.get("thread_id") == thread_id and (m["state"] == "running" or m.get("handed_off"))]
    if busy:
        what = "; ".join(f"{m['job_id']} {'running' if m['state'] == 'running' else 'handed to the human (handed_off)'}" for m in busy)
        if not force:
            raise RuntimeError(f"two writers on thread {thread_id}: {what} — codex_wait/codex_cancel, "
                               "codex_handoff(release=True) or force=True")
    reply = _launch(prompt, model, sandbox, cwd, name, effort, force, max_runtime_s, resume_thread=thread_id)
    if busy:
        reply["warnings"].append(f"force: continuing the thread although {what}")
    return reply


@mcp.tool()
def codex_jobs(limit: int = 20) -> list[dict]:
    """Recent jobs (newest first): state, age, thread_id, prompt truncated."""
    now = time.time()
    return [{"job_id": m["job_id"], "name": m.get("name"), "state": m["state"],
             "age_s": round(now - m["started_at"]), "thread_id": m.get("thread_id"),
             "prompt": _trunc(m["prompt"], 80)} for m in _all_jobs()[:limit]]


@mcp.tool()
def codex_limits() -> dict:
    """ChatGPT subscription quota: five_hour + weekly (used_percent, reset_at), cached 60 s.
    available=false + reason when the endpoint does not answer (401 = expired token) or when
    CODEX_BRIDGE_NO_LIMITS is set — never an exception."""
    return _limits()


@mcp.tool()
def codex_handoff(job_id: str, cancel_first: bool = True, release: bool = False) -> dict:
    """Manual takeover by the human: stops the job if it is running (cancel_first), marks the thread
    handed_off (codex_resume refuses without force) and gives the exact `codex resume <thread_id>`
    command. release=True removes the mark (the human is done; the bridge may resume the thread)."""
    d, meta = _job(job_id)
    out = {"job_id": meta["job_id"], "state": meta["state"], "thread_id": meta.get("thread_id")}
    if release:
        if meta.get("thread_id"):
            _mark_handed_off(meta["thread_id"], False)
        return {**out, "handed_off": False, "note": "mark removed; codex_resume is allowed again"}
    if meta["state"] == "running":
        if cancel_first:
            meta = _cancel(d, meta, "codex_handoff")
            out.update(state=meta["state"], cleanup_complete=meta.get("cleanup_complete"))
        else:
            out["warning"] = "job still running: two writers on the same thread — resume after it ends or cancel it"
    if not meta.get("thread_id"):
        out["note"] = ("thread_id still unknown (Codex did not emit thread.started) — there is no session to resume; "
                       "re-check with codex_status or look for the session in `codex resume` (interactive list)")
        return out
    _mark_handed_off(meta["thread_id"], True)
    out.update(handed_off=True, command=_resume_cmd(meta),
               note="Run the command in a terminal: the session shows up in Codex (TUI/app, `codex resume`) with the "
                    "full context of the run — prompt, tools used, replies. -C sets the working directory. "
                    "When you are done, call codex_handoff(job_id, release=True) so the bridge can resume the thread.")
    return out


if __name__ == "__main__":
    mcp.run()
