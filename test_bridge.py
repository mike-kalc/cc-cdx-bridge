#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mihai C.
"""codex-bridge tests: a fake `codex` shim on PATH — zero quota, zero external network.
`python test_bridge.py` runs tests 1–16; `python test_bridge.py --live` runs ONLY the live test
(one trivial prompt on the REAL Codex; consumes quota). POSIX only (macOS, Linux, WSL2)."""
import asyncio
import http.server
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(tempfile.mkdtemp(prefix="codex-bridge-test-"))
os.environ["CODEX_BRIDGE_JOBS"] = str(ROOT / "jobs")  # the tests' state never touches the real jobs/
BIN = ROOT / "bin"
BIN.mkdir()
REAL_PATH = os.environ["PATH"]
# The shim behaves like the real Codex: reads stdin UNTIL EOF (blocks if the pipe stays open),
# then emits JSONL following the schema in SPEC.md, writes -o and exits with the requested code.
(BIN / "codex").write_text(r'''#!/bin/sh
out=""; tid="thr_shim_$$"
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    resume) tid="$2"; shift ;;
  esac
  shift
done
echo "Reading additional input from stdin..." >&2
prompt=$(cat)
printf '{"type":"thread.started","thread_id":"%s"}\n' "$tid"
echo '{"type":"turn.started"}'
case "${CODEX_SHIM_MODE:-ok}" in
  slow) echo 'line that is not json'; sleep "${CODEX_SHIM_SLEEP:-20}" ;;
  fail) echo '{"type":"turn.failed","error":{"message":"boom"}}'; exit 1 ;;
  limit) echo '{"type":"error","message":"You have hit your usage limit."}'
         echo '{"type":"turn.failed","error":{"message":"usage limit"}}'; exit 1 ;;
esac
printf '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"echo: %s"}}\n' "$prompt"
echo '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0}}'
[ -n "$out" ] && printf 'echo: %s' "$prompt" > "$out"
exit 0
''')
(BIN / "codex").chmod(0o755)
os.environ["PATH"] = f"{BIN}:{REAL_PATH}"
sys.path.insert(0, str(HERE))
import server  # noqa: E402

USAGE_OK = {"rate_limit": {"primary_window": {"used_percent": 42.0, "reset_at": 1_700_000_000},
                           "secondary_window": {"used_percent": 10, "reset_at": 1_700_600_000}}}
TURN_OK = ('{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
           '{"type":"turn.completed","usage":{"output_tokens":1}}\n')


def wait(mod, job_id, timeout_s=10):
    return asyncio.run(mod.codex_wait(job_id, timeout_s=timeout_s))


def group_pids(pgid):
    out = subprocess.run(["ps", "-axo", "pid=,pgid="], capture_output=True, text=True).stdout
    return [int(p) for p, g in (l.split() for l in out.splitlines() if l.strip()) if int(g) == pgid]


def pid_alive(pid):
    return bool(server._lstart(pid))


def codex_pid(job_id):
    return json.loads((server.JOBS / job_id / "runner.json").read_text())["codex_pid"]


def shim(mode="ok", sleep=None):
    os.environ["CODEX_SHIM_MODE"] = mode
    if sleep is not None:
        os.environ["CODEX_SHIM_SLEEP"] = str(sleep)


def fake_job(name, events, end=None, pid=None, pid_start=None, **extra):
    """Synthetic job on disk (no process), for parser/classification/identity tests."""
    d = server.JOBS / name
    d.mkdir(parents=True)
    d.joinpath("events.jsonl").write_text(events)
    if end is not None:
        d.joinpath("exit.json").write_text(json.dumps(end))
    server._save(d, {"job_id": name, "prompt": "p", "pid": pid, "pid_start": pid_start,
                     "started_at": time.time() - 5, "state": "running", "max_runtime_s": 100, **extra})
    return d


def retire(d):
    """Closes a synthetic job that uses the test process's pid, so tearDown does not 'cancel' it (SIGTERM on ourselves)."""
    d.joinpath("exit.json").write_text(json.dumps({"exit_code": 0, "end_reason": "cancelled", "cleanup_complete": True}))


class Bridge(unittest.TestCase):
    def setUp(self):
        shim("ok")
        server._fetch_usage = lambda: USAGE_OK  # quota is simulated; no network, no auth.json
        server._LIMITS.update(at=0.0, data=None)

    def tearDown(self):
        for m in server._all_jobs():
            if m["state"] == "running" and m.get("pid") and m["pid"] != os.getpid():
                server.codex_cancel(m["job_id"])
        for p in server._PROCS.values():
            try:
                p.wait(timeout=5)  # runners have written exit.json; without a reap, unittest emits a ResourceWarning at exit
            except subprocess.TimeoutExpired:
                pass

    # 1. start returns immediately, even if the shim runs for 20 s
    def test_01_start_returns_fast(self):
        shim("slow", 20)
        t0 = time.time()
        r = server.codex_start("t1 long prompt", name="t1")
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(r["state"], "running")
        self.assertTrue(r["thread_id"].startswith("thr_shim_"), r)
        self.assertIn("codex resume thr_shim_", r["resume_command"])
        self.assertTrue(pid_alive(r["pid"]))

    # 2. stdin regression: the shim waits for EOF; an inherited/unclosed stdin => empty prompt or
    #    thread_id None after 15 s. The prompt goes through prompt.txt (0600), with no shell at all.
    def test_02_stdin_eof_regression(self):
        t0 = time.time()
        r = server.codex_start("t2 ping; echo $(id) 'with quotes' \"and doubles\"")
        self.assertLess(time.time() - t0, 5, "start waited for thread.started in vain: stdin left open?")
        self.assertIsNotNone(r["thread_id"], "the shim never got past cat: the child's stdin did not get EOF")
        self.assertEqual(wait(server, r["job_id"], 10)["state"], "succeeded")
        self.assertEqual(server.codex_result(r["job_id"])["result"],
                         "echo: t2 ping; echo $(id) 'with quotes' \"and doubles\"")
        d = server.JOBS / r["job_id"]
        self.assertIn("Reading additional input", server._tail(d / "stderr.log"))
        self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
        for f in ("prompt.txt", "meta.json", "events.jsonl", "exit.json"):
            self.assertEqual(stat.S_IMODE((d / f).stat().st_mode), 0o600, f)
        src = (HERE / "server.py").read_text() + (HERE / "runner.py").read_text()
        self.assertNotIn("shell=True", src)
        self.assertNotRegex(src, r'"sh",\s*"-c"|\bos\.system\(')

    # 3. status while running; a short wait returns running, not an error
    def test_03_status_and_short_wait(self):
        shim("slow", 6)
        r = server.codex_start("t3 slow")
        st = server.codex_status(r["job_id"])
        self.assertEqual(st["state"], "running")
        self.assertGreaterEqual(st["events"], 2)
        self.assertGreaterEqual(st["idle_s"], 0)
        self.assertFalse(st["stalled"])
        self.assertTrue(any(s.startswith("thread.started") for s in st["last_events"]))
        self.assertEqual(st["limits"]["five_hour"]["used_percent"], 42.0)
        t0 = time.time()
        st = wait(server, r["job_id"], 3)
        self.assertLess(time.time() - t0, 6)
        self.assertEqual(st["state"], "running")
        self.assertEqual(st["hint"], "call codex_wait again")
        self.assertEqual(server.codex_result(r["job_id"])["hint"], "job still running — codex_wait")
        self.assertEqual(wait(server, r["job_id"], 15)["state"], "succeeded")

    # 4. result + classification succeeded / failed / limit_reached
    def test_04_result_and_classification(self):
        ok = server.codex_start("t4 ok")
        shim("fail")
        bad = server.codex_start("t4 fail")
        shim("limit")
        lim = server.codex_start("t4 limit")
        res = {k: wait(server, j["job_id"]) and server.codex_result(j["job_id"]) for k, j in
               (("ok", ok), ("bad", bad), ("lim", lim))}
        self.assertEqual(res["ok"]["state"], "succeeded")
        self.assertEqual(res["ok"]["result"], "echo: t4 ok")
        self.assertEqual(res["ok"]["usage"]["output_tokens"], 5)
        self.assertEqual(res["ok"]["exit_code"], 0)
        self.assertIsNone(res["ok"]["error"])
        self.assertEqual(res["bad"]["state"], "failed")
        self.assertIn("boom", res["bad"]["error"])
        self.assertEqual(res["bad"]["exit_code"], 1)
        self.assertEqual(res["lim"]["state"], "limit_reached")
        self.assertIn("usage limit", res["lim"]["error"])  # the original message is kept
        self.assertIn("quota exhausted", res["lim"]["hint"])
        self.assertEqual(res["lim"]["limits"]["weekly"]["used_percent"], 10)
        jobs = server.codex_jobs()
        self.assertEqual({j["state"] for j in jobs if j["prompt"].startswith("t4")},
                         {"succeeded", "failed", "limit_reached"})

    # 5. cancel kills the WHOLE Codex group (shim + sleep) through the runner; watchdog in the runner (no polling)
    def test_05_cancel_kills_group(self):
        shim("slow", 60)
        r = server.codex_start("t5 cancel")
        cpid = codex_pid(r["job_id"])
        time.sleep(0.3)
        self.assertGreaterEqual(len(group_pids(cpid)), 2, "expecting shim + sleep in the Codex group")
        c = server.codex_cancel(r["job_id"])
        self.assertEqual(c["state"], "cancelled")
        self.assertTrue(c["cleanup_complete"])
        self.assertEqual(group_pids(cpid), [], "orphan processes after cancel")
        self.assertFalse(pid_alive(r["pid"]), "the runner must have exited")
        self.assertEqual(server.codex_status(r["job_id"])["state"], "cancelled")
        self.assertEqual(server.codex_cancel(r["job_id"])["note"], "job was not running")
        self.assertEqual(server._meta(server.JOBS / r["job_id"])["cancel_reason"], "codex_cancel")
        w = server.codex_start("t5 watchdog", max_runtime_s=1)
        time.sleep(3)  # NO status call meanwhile: the deadline is enforced by the runner
        self.assertEqual(json.loads((server.JOBS / w["job_id"] / "exit.json").read_text())["end_reason"], "timed_out")
        self.assertEqual(server.codex_status(w["job_id"])["state"], "timed_out")
        self.assertEqual(group_pids(codex_pid(w["job_id"])), [])

    # 6. dedupe on the hash and a cap of 3 running jobs
    def test_06_dedupe_and_cap(self):
        shim("slow", 20)
        a = server.codex_start("t6 a")
        a2 = server.codex_start("t6 a")
        self.assertEqual(a2["job_id"], a["job_id"])
        self.assertTrue(a2["deduplicated"])
        self.assertNotIn("deduplicated", a)
        b = server.codex_start("t6 b")
        self.assertNotEqual(b["job_id"], a["job_id"])
        server.codex_start("t6 c")
        with self.assertRaisesRegex(RuntimeError, "cap of 3 running") as cm:
            server.codex_start("t6 d")
        self.assertIn(a["job_id"], str(cm.exception))
        self.assertEqual(sum(j["state"] == "running" for j in server.codex_jobs()), 3)

    # 7. survival: a fresh module instance (no Popen) reads the state from disk
    def test_07_survives_server_restart(self):
        shim("slow", 3)
        r = server.codex_start("t7 restart")
        spec = importlib.util.spec_from_file_location("server_fresh", HERE / "server.py")
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        fresh._fetch_usage = lambda: USAGE_OK
        self.assertEqual(fresh._PROCS, {})
        st = fresh.codex_status(r["job_id"])
        self.assertEqual((st["state"], st["thread_id"]), ("running", r["thread_id"]))
        self.assertEqual(wait(fresh, r["job_id"], 10)["state"], "succeeded")
        self.assertEqual(fresh.codex_result(r["job_id"])["result"], "echo: t7 restart")
        self.assertIn(r["job_id"], [j["job_id"] for j in fresh.codex_jobs()])
        # resume after the "restart": the thread continues, the new job is separate
        shim("ok")
        r2 = fresh.codex_resume(r["job_id"], "t7 continuation")
        self.assertNotEqual(r2["job_id"], r["job_id"])
        self.assertEqual(r2["thread_id"], r["thread_id"])
        self.assertEqual(wait(fresh, r2["job_id"])["state"], "succeeded")
        self.assertEqual(fresh._meta(fresh.JOBS / r2["job_id"])["resume_of"], r["thread_id"])
        with self.assertRaises(ValueError):
            fresh.codex_status("does-not-exist")
        fresh._PROCS[r2["job_id"]].wait(timeout=5)  # tearDown only reaps server._PROCS

    # 8. tolerant parser: invalid lines, unknown types, missing fields, empty file
    def test_08_tolerant_parser(self):
        d = fake_job("20000101-000000-t8", "\n".join([
            "", "plain text", "{not json", "[1,2,3]", '"string"',
            '{"type":"thread.started"}', '{"type":"item.completed","item":"not a dict"}',
            '{"type":"item.completed","item":{"type":"command_execution","command":"ls -la"}}',
            '{"type":"new.type","payload":{"a":1,"b":"xxxxx"}}', '{"no":"type"}', '{"type":5}',
            '{"type":"turn.completed","usage":"weird"}', '{"type":"error"}',
            '{"type":"turn.failed","error":"plain string"}', '{"type":"turn.completed"}',
        ]) + "\n", end={"exit_code": 0, "end_reason": "ok", "cleanup_complete": True})
        self.assertEqual(len(server._events(d)), 10)
        st = server.codex_status(d.name)
        self.assertEqual(st["state"], "failed")  # exit 0 but with turn.failed
        self.assertEqual(len(st["last_events"]), 8)
        self.assertTrue(all(isinstance(s, str) for s in st["last_events"]))
        self.assertIn("item.completed [command_execution] ls -la", st["last_events"])
        self.assertIn("new.type", " ".join(st["last_events"]))
        self.assertIn("plain string", st["error"])
        for e in ({}, {"type": None}, {"item": None}, {"type": "item.started"}, {"type": "x", "v": "y" * 500}):
            self.assertIsInstance(server._summary(e), str)
        self.assertLess(len(server._summary({"type": "x", "v": "y" * 500})), 150)
        e = fake_job("20000101-000001-t8-empty", "", started_at=time.time() - 100)  # no pid and older than 30 s
        st = server.codex_status(e.name)
        self.assertEqual((st["state"], st["events"], st["last_events"]), ("lost", 0, []))
        self.assertIsNotNone(st["hint"])

    # 9. limits with a simulated response: 5h/weekly parsing, 401 -> available:false, cache, quota guard
    def test_09_limits_simulated(self):
        lim = server.codex_limits()
        self.assertEqual(lim, {"available": True, "limit_reached": False,
                               "five_hour": {"used_percent": 42.0, "reset_at": server._iso(1_700_000_000)},
                               "weekly": {"used_percent": 10, "reset_at": server._iso(1_700_600_000)}})

        def unauthorized():
            raise urllib.error.HTTPError(server.USAGE_URL, 401, "Unauthorized", {}, io.BytesIO())
        server._fetch_usage = unauthorized
        self.assertTrue(server.codex_limits()["available"])  # 60 s cache
        self.assertEqual(server._limits(fresh=True), {"available": False, "reason": "token expired (run any `codex` command to refresh it)"})
        server._fetch_usage = lambda: {"rate_limit": {"primary_window": {"used_percent": 96, "reset_at": "not-an-epoch"}}}
        lim = server._limits(fresh=True)
        self.assertTrue(lim["limit_reached"])
        self.assertEqual(lim["five_hour"], {"used_percent": 96, "reset_at": None})
        self.assertEqual(lim["weekly"], {"used_percent": None, "reset_at": None})
        with self.assertRaisesRegex(RuntimeError, "force=True"):
            server.codex_start("t9 refused")
        r = server.codex_start("t9 forced", force=True)
        self.assertIn("96", r["warnings"][0])
        server._fetch_usage = lambda: {"something": 1}
        self.assertEqual(server._limits(fresh=True), {"available": False, "reason": "ValueError"})
        r = server.codex_start("t9 without guard")
        self.assertIn("without the guard", r["warnings"][0])
        self.assertEqual(wait(server, r["job_id"])["state"], "succeeded")

    # 10. umask + opener without redirects: Authorization does NOT leave after a 302, errors are redacted
    def test_10_limits_no_redirect_and_perms(self):
        self.assertEqual(os.umask(0o077), 0o077)
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append((self.path, dict(self.headers)))
                if self.path == "/usage":
                    self.send_response(302)
                    self.send_header("Location", "/leak")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps(USAGE_OK).encode())

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        auth = ROOT / "auth-fake.json"  # NOT ~/.codex/auth.json
        auth.write_text(json.dumps({"tokens": {"access_token": "tok-fake-123", "account_id": "acc-fake"}}))
        # fresh module: the REAL _fetch_usage (in `server`, setUp put a lambda over it)
        spec = importlib.util.spec_from_file_location("server_real", HERE / "server.py")
        real = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(real)
        real.USAGE_URL, real.AUTH = f"http://127.0.0.1:{srv.server_port}/usage", auth
        try:
            self.assertEqual(real._limits(fresh=True), {"available": False, "reason": "HTTP 302"})
            self.assertEqual([h[0] for h in hits], ["/usage"], "the redirect was followed")
            self.assertEqual(hits[0][1]["Authorization"], "Bearer tok-fake-123")
            real.USAGE_URL = f"http://127.0.0.1:{srv.server_port}/direct"
            self.assertEqual(real._limits(fresh=True)["five_hour"]["used_percent"], 42.0)
            real.AUTH = ROOT / "missing.json"
            self.assertEqual(real._limits(fresh=True), {"available": False, "reason": "FileNotFoundError"})
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertNotIn("tok-fake", json.dumps(real._LIMITS))

    # 11. pid identity: a reused pid (different start time) => lost and NO signal
    def test_11_pid_identity(self):
        me = os.getpid()
        bogus = fake_job("20000101-000002-t11-bogus", "", pid=me, pid_start="Mon Jan  1 00:00:00 2000")
        c = server.codex_cancel(bogus.name)  # if it signalled the "reused" pid, the test process would die here
        self.assertEqual((c["state"], c["note"]), ("lost", "job was not running"))
        st = server.codex_status(bogus.name)
        self.assertEqual(st["state"], "lost")
        self.assertIn("reused pid", st["hint"])
        self.assertNotIn("cancel_reason", server._meta(bogus))
        same = fake_job("20000101-000004-t11-same", "", pid=me, pid_start=server._lstart(me))
        self.assertEqual(server.codex_status(same.name)["state"], "running")
        retire(same)
        self.assertFalse(server._alive({"job_id": "x", "pid": None, "pid_start": None}))
        self.assertFalse(server._alive({"job_id": "x", "pid": me, "pid_start": ""}))

    # 12. flock between concurrent starters: the same prompt from 2 threads => a single job; effort is in the key
    def test_12_lock_and_dedupe_key(self):
        shim("slow", 20)
        out = []
        ts = [threading.Thread(target=lambda: out.append(server.codex_start("t12 concurrent"))) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(len({r["job_id"] for r in out}), 1, out)
        self.assertEqual(sum(bool(r.get("deduplicated")) for r in out), 1)
        self.assertTrue((server.JOBS / ".lock").exists())
        lo = server.codex_start("t12 effort", effort="low")
        hi = server.codex_start("t12 effort", effort="high")
        self.assertNotEqual(lo["job_id"], hi["job_id"])
        self.assertEqual(server._meta(server.JOBS / hi["job_id"])["argv"][-3:], ["-c", "model_reasoning_effort=high", "-"])

    # 13. two writers on the same thread: running or handed_off => resume refused; force; release
    def test_13_handoff_marks_thread(self):
        a = server.codex_start("t13 source")
        self.assertEqual(wait(server, a["job_id"])["state"], "succeeded")
        tid = a["thread_id"]
        shim("slow", 20)
        r1 = server.codex_resume(tid, "t13 continuation 1")
        self.assertEqual(r1["thread_id"], tid)
        with self.assertRaisesRegex(RuntimeError, "two writers.*running"):
            server.codex_resume(a["job_id"], "t13 continuation 2")
        h = server.codex_handoff(r1["job_id"])
        self.assertEqual((h["state"], h["handed_off"], h["command"]), ("cancelled", True, f"codex resume {tid}"))
        self.assertTrue(server.codex_status(a["job_id"])["handed_off"])
        with self.assertRaisesRegex(RuntimeError, "handed_off"):
            server.codex_resume(tid, "t13 continuation 3")
        shim("ok")
        forced = server.codex_resume(tid, "t13 forced", force=True)
        self.assertIn("force", forced["warnings"][0])
        self.assertEqual(wait(server, forced["job_id"])["state"], "succeeded")
        rel = server.codex_handoff(a["job_id"], release=True)
        self.assertFalse(rel["handed_off"])
        self.assertFalse(any(j for j in server._all_jobs() if j.get("thread_id") == tid and j.get("handed_off")))
        ok = server.codex_resume(tid, "t13 after release")
        self.assertEqual(ok["warnings"], [])
        self.assertEqual(wait(server, ok["job_id"])["state"], "succeeded")
        # handoff on a job without a thread_id says so explicitly
        d = fake_job("20000101-000005-t13-no-thread", "", end={"exit_code": 1, "end_reason": "failed"})
        self.assertIn("thread_id still unknown", server.codex_handoff(d.name)["note"])

    # 14. complete lines only: a thread.started cut in half gives no error and is not lost
    def test_14_partial_line(self):
        d = fake_job("20000101-000006-t14", '{"type":"thread.started","thread_id":"thr_part',
                     pid=os.getpid(), pid_start=server._lstart(os.getpid()))
        self.assertEqual(server._events(d), [])
        st = server.codex_status(d.name)
        self.assertEqual((st["state"], st["thread_id"], st["events"]), ("running", None, 0))
        with open(d / "events.jsonl", "a") as f:
            f.write('ial"}\n{"type":"turn.started"}\n{"type":"item.started","item":{"type":"x"}')
        st = server.codex_status(d.name)
        self.assertEqual((st["thread_id"], st["events"]), ("thr_partial", 2))
        h = server.codex_handoff(d.name, cancel_first=False)
        self.assertEqual(h["command"], "codex resume thr_partial")
        self.assertIn("two writers", h["warning"])
        retire(d)

    # 15. limit_reached only on explicit phrasings / structured code; transient 429 + success = succeeded
    def test_15_limit_classification(self):
        cases = {
            "disk": ('{"type":"turn.failed","error":{"message":"write failed: disk quota exceeded"}}\n', 1, "failed"),
            "code": ('{"type":"error","error":{"code":"insufficient_quota","message":"x"}}\n', 1, "limit_reached"),
            "rate": ('{"type":"turn.failed","error":{"message":"Rate limit reached for gpt-5"}}\n', 1, "limit_reached"),
            "s429": ('{"type":"turn.failed","error":{"message":"unexpected status 429 Too Many Requests"}}\n', 1, "limit_reached"),
            "num":  ('{"type":"turn.failed","error":{"message":"wrote 429 bytes then crashed"}}\n', 1, "failed"),
            "tran": ('{"type":"error","message":"status 429, retrying"}\n' + TURN_OK, 0, "succeeded"),
        }
        for name, (events, code, want) in cases.items():
            d = fake_job(f"20000101-000007-t15-{name}", '{"type":"thread.started","thread_id":"t"}\n' + events,
                         end={"exit_code": code, "end_reason": "ok" if code == 0 else "failed"})
            st = server.codex_status(d.name)
            self.assertEqual(st["state"], want, name)
            if want != "succeeded":
                self.assertIn(json.loads(events.splitlines()[0]).get("error", {}).get("message", "x")[:10], st["error"])
        self.assertIsNone(server.LIMIT_RE.search("quota"))

    # 16. opt-out: CODEX_BRIDGE_NO_LIMITS never reads auth.json and never calls the network
    def test_16_limits_optout(self):
        def boom():
            raise AssertionError("quota fetched although disabled")
        server._fetch_usage = boom
        os.environ["CODEX_BRIDGE_NO_LIMITS"] = "1"
        try:
            self.assertEqual(server.codex_limits(), {"available": False, "reason": "disabled (CODEX_BRIDGE_NO_LIMITS)"})
            r = server.codex_start("t16 no limits")
            self.assertEqual(r["warnings"], [])
            self.assertEqual(wait(server, r["job_id"])["state"], "succeeded")
        finally:
            os.environ.pop("CODEX_BRIDGE_NO_LIMITS", None)


def live():
    """One single trivial prompt on the REAL Codex (consumes quota) — only on explicit request (--live)."""
    os.environ["PATH"] = REAL_PATH
    os.environ.pop("CODEX_SHIM_MODE", None)
    print("limits:", json.dumps(server.codex_limits()))
    r = server.codex_start("Reply with the single word OK.", name="live-smoke")
    print("start:", json.dumps(r))
    st = wait(server, r["job_id"], 110)
    while st["state"] == "running":
        print("...", st["last_events"][-1:] or "", f"{st['elapsed_s']}s")
        st = wait(server, r["job_id"], 110)
    res = server.codex_result(r["job_id"])
    print("result:", json.dumps({k: res[k] for k in ("state", "result", "usage", "duration_s", "thread_id", "resume_command", "limits", "error")}, ensure_ascii=False))
    assert res["state"] == "succeeded" and "OK" in (res["result"] or "").upper(), res


if __name__ == "__main__":
    if "--live" in sys.argv:
        live()
    else:
        unittest.main(verbosity=2)
