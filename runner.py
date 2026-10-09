#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mihai C.
"""Detached codex-bridge runner: `runner.py <job_dir>`.

Starts Codex (argv from meta.json, prompt from prompt.txt on stdin) in its own process group,
enforces max_runtime_s and writes exit.json ATOMICALLY at the end — independent of the MCP
server, which may die or restart in the meantime. SIGTERM (from codex_cancel) = stop the Codex
group (TERM, 5 s, KILL) and record end_reason=cancelled.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)  # the reader sees either nothing or the whole file


def _group_empty(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        # macOS: transient EPERM while a member re-parented to launchd is not yet reaped
        return False


def _killpg(p: subprocess.Popen) -> bool:
    """TERM, 5 s, KILL on the whole Codex group; True if the group is empty at the end."""
    for sig, patience in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(p.pid, sig)
        except ProcessLookupError:
            return True
        for _ in range(int(patience * 10)):
            p.poll()  # reap the leader: a zombie would keep the group "alive"
            if _group_empty(p.pid):
                return True
            time.sleep(0.1)
    return False


def main(job: Path) -> None:
    os.umask(0o077)
    meta = json.loads((job / "meta.json").read_text())
    cancelled = []
    signal.signal(signal.SIGTERM, lambda *_: cancelled.append(1))
    try:
        # stdin from a file: natural EOF (an unclosed pipe would make `codex exec -` wait forever),
        # no size limit, no shell interpolation — argv goes straight to execve
        with open(job / "prompt.txt", "rb") as fin, open(job / "events.jsonl", "ab") as out, \
                open(job / "stderr.log", "ab") as err:
            p = subprocess.Popen(meta["argv"], stdin=fin, stdout=out, stderr=err, start_new_session=True)
    except Exception as e:  # e.g. `codex` missing from PATH
        _write(job / "exit.json", {"exit_code": 127, "end_reason": "failed", "cleanup_complete": True,
                                   "error": f"{type(e).__name__}: {e}"})
        return
    _write(job / "runner.json", {"codex_pid": p.pid})
    deadline = time.time() + float(meta.get("max_runtime_s") or 7200)
    reason = None
    while p.poll() is None:
        if cancelled:
            reason = "cancelled"
            break
        if time.time() > deadline:
            reason = "timed_out"
            break
        time.sleep(0.2)
    clean = _killpg(p) if reason else _group_empty(p.pid)
    code = p.wait()
    _write(job / "exit.json", {"exit_code": code, "end_reason": reason or ("ok" if code == 0 else "failed"),
                               "cleanup_complete": clean})


if __name__ == "__main__":
    main(Path(sys.argv[1]))
