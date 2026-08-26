#!/usr/bin/env python3
"""Deterministic stdlib regression test for the nodoom.composer backend.

Runs the real backend.py as a subprocess inside a throwaway sandbox with
a private HOME, XDG_RUNTIME_DIR and PATH, so no real config, browser,
network or clipboard is ever touched: the fake xdg-open and wl-copy
found on the sandbox PATH record their argv / stdin instead of opening
anything. Backend path is repository-relative.

Usage:  python3 tests/test_backend.py   (from the plugin root)

Prints "backend regression: PASS" and exits 0 on success; the first
failed assertion aborts with full subprocess output.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend.py"
TERMINAL = {"posted", "handoff", "rejected", "unknown"}


def run(env, *args, payload=None, ok=True):
    p = subprocess.run(
        [sys.executable, str(BACKEND), *args],
        input=None if payload is None else json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    data = json.loads(p.stdout)
    if ok is True:
        assert p.returncode == 0 and data.get("ok") is True, (p.returncode, data, p.stderr)
    if ok is False:
        assert p.returncode != 0 and data.get("ok") is False, (p.returncode, data, p.stderr)
    return p.returncode, data


def wait(env, jid):
    for _ in range(100):
        _, st = run(env, "status", jid, ok=True)
        if st.get("state") in TERMINAL:
            return st
        time.sleep(0.05)
    raise AssertionError("job did not terminate")


with tempfile.TemporaryDirectory(prefix="npost-reg-") as td:
    root = Path(td)
    home = root / "home"
    runtime = root / "run"
    bin = root / "bin"
    count = root / "opens"
    arg = root / "open-arg"
    clip = root / "clipboard"
    for p in (home, runtime, bin):
        p.mkdir(mode=0o700)
    (bin / "xdg-open").write_text(
        "\n".join(
            [
                "#!/bin/sh",
                f"printf x >> {count}",
                f"printf '%s' \"$1\" > {arg}",
                "sleep 1",
                "exit 0",
                "",
            ]
        )
    )
    (bin / "wl-copy").write_text(
        "\n".join(
            [
                "#!/bin/sh",
                f"cat > {clip}",
                "exit 0",
                "",
            ]
        )
    )
    os.chmod(bin / "xdg-open", 0o755)
    os.chmod(bin / "wl-copy", 0o755)
    env = os.environ.copy()
    env.update(
        HOME=str(home),
        XDG_RUNTIME_DIR=str(runtime),
        PATH=f"{bin}:{env['PATH']}",
    )

    # First run provisions the config; mode is always the free browser
    # composer and the provisioned paths keep owner-only permission bits.
    _, m = run(env, "mode", ok=True)
    assert m["mode"] == "intent" and m["paid"] is False, m
    assert m.get("copyDraft") is True, m
    cfg_dir = home / ".config" / "npost"
    cfg_file = cfg_dir / "config.toml"
    assert os.lstat(cfg_dir).st_mode & 0o777 == 0o700, "config dir is not 0700"
    assert os.lstat(cfg_file).st_mode & 0o777 == 0o600, "config.toml is not 0600"

    # Oversize posts are refused before a job is created.
    rc, too_long = run(env, "enqueue", payload={"text": "n" * 5001}, ok=False)
    assert too_long["kind"] == "input", too_long

    # Detached worker: exactly one claim, replay is refused as busy, clipboard
    # receives the draft on stdin, and xdg-open's argv is a private file URI
    # that does not contain the draft. The redirect HTML carries
    # /composer?text= so Nodoom prefills the textarea.
    _, q = run(env, "enqueue", payload={"text": "one worker only"}, ok=True)
    jid = q["jobId"]
    worker_claim = runtime / "nodoom.composer" / "jobs" / jid / "worker.json"
    for _ in range(50):
        if worker_claim.exists():
            break
        time.sleep(0.02)
    assert worker_claim.exists(), "detached worker never claimed the job"
    replay = subprocess.run(
        [sys.executable, str(BACKEND), "_worker", jid],
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
    )
    if replay.stdout.strip():
        replay_obj = json.loads(replay.stdout)
        assert replay.returncode != 0 or replay_obj.get("kind") == "busy"
    st = wait(env, jid)
    assert st["state"] == "handoff", st
    assert st.get("copied") is True, st
    assert count.read_text() == "x", count.read_text()
    assert clip.read_text() == "one worker only", clip.read_text()
    opened_arg = arg.read_text()
    assert opened_arg.startswith("file://") and "one%20worker%20only" not in opened_arg, opened_arg
    redirect = Path(urllib.parse.unquote(urllib.parse.urlparse(opened_arg).path))
    redirect_html = redirect.read_text()
    assert "https://nodoom.app/composer?" in redirect_html
    assert "one%20worker%20only" in redirect_html
    run(env, "ack", jid, ok=True)
    assert not redirect.exists(), "ack retained draft-bearing redirect"

    worker_files = list((runtime / "nodoom.composer" / "jobs").glob("*/worker.json"))
    assert not worker_files, worker_files

    # copy_draft = false: no clipboard write; the redirect still carries ?text=.
    cfg_file.write_text("copy_draft = false\n")
    count.write_text("")
    clip.write_text("")
    _, q = run(env, "enqueue", payload={"text": "no clip"}, ok=True)
    jid = q["jobId"]
    st = wait(env, jid)
    assert st["state"] == "handoff" and not st.get("copied"), st
    opened_arg = arg.read_text()
    redirect = Path(urllib.parse.unquote(urllib.parse.urlparse(opened_arg).path))
    redirect_html = redirect.read_text()
    assert "https://nodoom.app/composer?" in redirect_html and "no%20clip" in redirect_html
    assert "no clip" not in opened_arg
    assert clip.read_text() == ""
    run(env, "ack", jid, ok=True)
    cfg_file.write_text("copy_draft = true\n")

    # Draft flow: enqueue pins the draft revision it submitted, a second
    # enqueue while the terminal result is unacked is refused as busy, and
    # clearing with a stale revision is a CAS conflict that preserves the
    # saved draft.
    count.write_text("")
    clip.write_text("")
    _, d1 = run(env, "draft", "set", payload={"text": "A"}, ok=True)
    rev1 = d1["revision"]
    assert os.lstat(cfg_dir / "draft.json").st_mode & 0o777 == 0o600, "draft.json is not 0600"
    _, q = run(env, "enqueue", payload={"text": "A"}, ok=True)
    jid = q["jobId"]
    assert q["draftRevision"] == rev1
    run(env, "draft", "set", payload={"text": "B"}, ok=True)
    _, d3 = run(env, "draft", "set", payload={"text": "A"}, ok=True)
    rev3 = d3["revision"]
    assert rev3 > rev1
    st = wait(env, jid)
    assert st["state"] == "handoff" and st["draftRevision"] == rev1, st
    rc, busy = run(env, "enqueue", payload={"text": "duplicate"}, ok=False)
    assert busy["kind"] == "busy" and busy["jobId"] == jid, busy
    rc, conflict = run(env, "draft", "clear", str(rev1), ok=False)
    assert conflict["kind"] == "conflict", conflict
    _, saved = run(env, "draft", "get", ok=True)
    assert saved["text"] == "A" and saved["revision"] == rev3, saved
    run(env, "ack", jid, ok=True)

    # Concurrent clear vs set on the same revision: the draft lock
    # serializes them, the set always survives, and the clear either wins
    # its compare-and-swap or is refused with a conflict.
    for i in range(20):
        _, base = run(env, "draft", "set", payload={"text": "A"}, ok=True)
        rev = base["revision"]
        clear = subprocess.Popen(
            [sys.executable, str(BACKEND), "draft", "clear", str(rev)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        setter = subprocess.Popen(
            [sys.executable, str(BACKEND), "draft", "set"],
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        set_out, set_err = setter.communicate(json.dumps({"text": "B"}), timeout=5)
        clear_out, clear_err = clear.communicate(timeout=5)
        assert setter.returncode == 0, (set_out, set_err)
        assert clear.returncode in (0, 1), (clear_out, clear_err)
        _, saved = run(env, "draft", "get", ok=True)
        assert saved["text"] == "B", saved

print("backend regression: PASS")
