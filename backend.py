#!/usr/bin/env python3
"""nodoom.composer backend — job-based posting helper for the Omarchy plugin.

Stdlib-only, single file. Every invocation prints exactly ONE JSON object on
stdout (diagnostics go to stderr) and exits 0 for {"ok": true}, nonzero
otherwise. Post text always arrives via stdin or job files — never via
shell interpolation or command-line arguments.

Commands
    mode                        -> {ok, mode:"intent", label, paid:false, copyDraft}
    enqueue  (stdin {text})     -> {ok, jobId, mode} | {ok:false, kind:"busy", jobId}
    status   JOBID              -> {ok, jobId, state, mode, message, submittedText?}
    active                      -> {ok, active:<status>|null}
    ack      JOBID              -> {ok, jobId}
    draft get|set|clear         -> {ok[, text]}
    _worker  JOBID              (internal — detached job worker)

Job states: queued | running | posted | handoff | rejected | unknown.
The first two are nonterminal; the rest are terminal and persist until
"ack". The worker is spawned detached and communicates only through state
files, so jobs survive shell reloads and re-attach on the next poll.

Mode
    intent (only): hands the draft to Nodoom in the browser at
        https://nodoom.app/composer?text=… via a private HTML redirect
        (xdg-open never sees the draft). Optionally copies the draft to
        the clipboard as a backup. The user presses Post there. There is
        no API posting path — Nodoom has no public write API.

Security posture
    * ~/.config/npost is kept 0700 and config.toml 0600; symlinks, foreign
      owners, or group/other permission bits are refused.
    * Job state lives under $XDG_RUNTIME_DIR/nodoom.composer (0700) with 0600
      files.
    * The handoff URL may embed draft text and is never logged, echoed,
      or returned in JSON (handoff reports carry no URL).
"""

from __future__ import annotations

import contextlib
import fcntl
import html
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.parse
import uuid
from pathlib import Path

INTENT_URL = "https://nodoom.app/composer"
USER_AGENT = "nodoom.composer-omarchy/1.0"
XDG_OPEN_TIMEOUT = 20  # seconds
WORKER_NEVER_STARTED_GRACE = 60  # seconds
STALE_JOB_AGE = 6 * 3600  # any job older than this is reaped
GC_AGE = 7 * 86400  # finished job dirs are garbage-collected after a week
MAX_TEXT = 5000  # Nodoom composer content maxLength

TERMINAL_STATES = frozenset({"posted", "handoff", "rejected", "unknown"})
_JOB_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-f0-9]{8}$")

BACKEND_PATH = Path(__file__).resolve()
PLUGIN_DIR = BACKEND_PATH.parent
CONFIG_DIR = Path.home() / ".config" / "npost"
CONFIG_FILE = CONFIG_DIR / "config.toml"
DRAFT_FILE = CONFIG_DIR / "draft.json"

CONFIG_TEMPLATE = """\
# npost configuration
# Path: ~/.config/npost/config.toml   (keep the directory 0700 and this
# file 0600.)
#
# Generated on first run; edit freely, it is never overwritten.
#
# Browser composer (the only mode)
#    Posts are handed off to Nodoom at
#    https://nodoom.app/composer?text=…  (Nodoom prefills the textarea).
#    Nothing is published until you press Post there.
#
# copy_draft = true  ->  the draft is also copied to the clipboard with
#    wl-copy (Wayland), xclip, or xsel, as a backup if the URL prefill
#    is empty. Press Ctrl+V in that case.

copy_draft = true
"""


# ------------------------------------------------------------------ output


class BackendError(Exception):
    """Carries a JSON-serializable failure kind + message."""

    def __init__(self, kind: str, message: str, **extra: object) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.extra = extra


def emit(obj: dict, code: int = 0) -> int:
    """Print exactly one JSON object and return the exit code."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
    return code


def _log(message: str) -> None:  # worker diagnostics; never log text or URLs
    sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {message}\n")
    sys.stderr.flush()


# ------------------------------------------------------------ file helpers


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Durably replace `path` with `data`, owner-only."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: Path) -> object | None:
    """Read a JSON file without following symlinks; None if absent/broken."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        raw = f.read()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ------------------------------------------------------------------ config


def _refuse_bad_path(p: Path, want_dir: bool, what: str) -> None:
    st = os.lstat(p)
    if stat.S_ISLNK(st.st_mode):
        raise BackendError("config", f"{what} is a symlink — refusing; replace it with a real {'directory' if want_dir else 'file'}")
    if st.st_uid != os.geteuid():
        raise BackendError("config", f"{what} is not owned by you (uid {st.st_uid}) — refusing")
    bits = stat.S_IMODE(st.st_mode)
    if bits & 0o077:
        want = "700" if want_dir else "600"
        raise BackendError("config", f"{what} has group/other permission bits ({oct(bits)}) — refusing; run: chmod {want} {p}")
    if want_dir and not stat.S_ISDIR(st.st_mode):
        raise BackendError("config", f"{what} is not a directory — refusing")
    if not want_dir and not stat.S_ISREG(st.st_mode):
        raise BackendError("config", f"{what} is not a regular file — refusing")


def ensure_config_files() -> None:
    """Guarantee ~/.config/npost (0700) + config.toml (0600), or refuse."""
    if not CONFIG_DIR.exists():
        with contextlib.suppress(FileExistsError):
            CONFIG_DIR.mkdir(mode=0o700, parents=True)
        os.chmod(CONFIG_DIR, 0o700)
    _refuse_bad_path(CONFIG_DIR, True, "~/.config/npost")
    if not CONFIG_FILE.exists():
        atomic_write(CONFIG_FILE, CONFIG_TEMPLATE.encode("utf-8"), 0o600)
    _refuse_bad_path(CONFIG_FILE, False, "~/.config/npost/config.toml")


def load_config() -> dict:
    ensure_config_files()
    try:
        fd = os.open(CONFIG_FILE, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        raise BackendError("config", f"cannot read {CONFIG_FILE}: {e.strerror}") from e
    with os.fdopen(fd, "rb") as f:
        try:
            cfg = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise BackendError("config", f"~/.config/npost/config.toml is not valid TOML: {e}") from e
    return cfg if isinstance(cfg, dict) else {}


def compute_mode(cfg: dict) -> dict:
    copy_draft = cfg.get("copy_draft", True)
    if not isinstance(copy_draft, bool):
        raise BackendError("config", "copy_draft must be true or false")
    return {
        "mode": "intent",
        "paid": False,
        "label": "Browser composer",
        "copyDraft": copy_draft,
    }


# ------------------------------------------------------------ runtime/jobs


def ensure_runtime() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR", "")
    if not base or not os.path.isabs(base):
        raise BackendError("runtime", "XDG_RUNTIME_DIR is not set; cannot manage job state")
    rt = Path(base) / "nodoom.composer"
    if rt.exists():
        _refuse_bad_path(rt, True, "$XDG_RUNTIME_DIR/nodoom.composer")
    else:
        with contextlib.suppress(FileExistsError):
            rt.mkdir(mode=0o700)
        os.chmod(rt, 0o700)
    jobs = rt / "jobs"
    if jobs.exists():
        _refuse_bad_path(jobs, True, "$XDG_RUNTIME_DIR/nodoom.composer/jobs")
    else:
        with contextlib.suppress(FileExistsError):
            jobs.mkdir(mode=0o700)
        os.chmod(jobs, 0o700)
    return rt


def require_job_id(argv: list[str]) -> str:
    if len(argv) != 1 or not _JOB_ID_RE.match(argv[0]):
        raise BackendError("usage", "expected a job id (see `active`)")
    return argv[0]


def new_job_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _job_dir(rt: Path, jid: str) -> Path:
    return rt / "jobs" / jid


def _read_active(rt: Path) -> str | None:
    try:
        with open(rt / "active", "r", encoding="ascii") as f:
            jid = f.read().strip()
    except OSError:
        return None
    return jid if _JOB_ID_RE.match(jid) else None


def _write_active(rt: Path, jid: str) -> None:
    atomic_write(rt / "active", (jid + "\n").encode("ascii"), 0o600)


def _clear_active(rt: Path, jid: str | None = None) -> None:
    if jid is None or _read_active(rt) == jid:
        with contextlib.suppress(OSError):
            os.unlink(rt / "active")


def _load_job(rt: Path, jid: str) -> dict | None:
    data = read_json(_job_dir(rt, jid) / "state.json")
    return data if isinstance(data, dict) else None


def _write_state(rt: Path, jid: str, **fields: object) -> dict:
    jd = _job_dir(rt, jid)
    state = read_json(jd / "state.json")
    state = state if isinstance(state, dict) else {"jobId": jid}
    state.update(fields)
    state["jobId"] = jid
    state["updated"] = time.time()
    atomic_write(jd / "state.json", json.dumps(state).encode("utf-8"), 0o600)
    return state


def _reap(rt: Path, jid: str, st: dict) -> dict:
    """Mark orphaned nonterminal jobs unknown (worker died / never started)."""
    if st.get("state") in TERMINAL_STATES:
        return st
    worker = read_json(_job_dir(rt, jid) / "worker.json")
    pid = worker.get("pid") if isinstance(worker, dict) else None
    age = time.time() - float(st.get("created") or 0)
    dead = (
        (pid is None and age > WORKER_NEVER_STARTED_GRACE)
        or (pid is not None and not _pid_alive(pid))
        or age > STALE_JOB_AGE
    )
    if not dead:
        return st
    if pid is not None:
        # The worker is gone; let any final terminal write become visible.
        time.sleep(0.15)
        st = _load_job(rt, jid) or st
        if st.get("state") in TERMINAL_STATES:
            return st
    why = "worker exited before finishing" if pid is not None else "worker never started"
    return _write_state(rt, jid, state="unknown", message=f"{why} — result unknown; draft kept")


def _finish_job(rt: Path, jid: str) -> None:
    """Ack-side cleanup: remove all draft-bearing job material."""
    _clear_active(rt, jid)
    shutil.rmtree(_job_dir(rt, jid), ignore_errors=True)


def _gc_old_jobs(rt: Path) -> None:
    now = time.time()
    with contextlib.suppress(OSError):
        for e in (rt / "jobs").iterdir():
            try:
                age = now - e.stat().st_mtime
                if e.is_dir() and age > GC_AGE:
                    shutil.rmtree(e, ignore_errors=True)
                elif e.is_file() and age > 3600:
                    e.unlink()
            except OSError:
                pass
    with contextlib.suppress(OSError):
        for e in rt.iterdir():
            try:
                if e.is_file() and e.name.startswith(".tmp-") and now - e.stat().st_mtime > 3600:
                    e.unlink()
            except OSError:
                pass


@contextlib.contextmanager
def _global_lock(rt: Path):
    """Serialize job reservation with a kernel-owned, crash-safe file lock."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(rt / "lock", flags, 0o600)
    except OSError as e:
        raise BackendError("internal", f"cannot open the job lock: {e.strerror}") from e
    try:
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + 5.0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BackendError("internal", "job lock stayed busy — try again")
                time.sleep(0.05)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ------------------------------------------------------------------ posting


def _copy_to_clipboard(text: str) -> bool:
    """Copy `text` via stdin (never argv). Returns True on success."""
    candidates = (
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    )
    payload = text.encode("utf-8")
    for cmd in candidates:
        exe = shutil.which(cmd[0])
        if exe is None:
            continue
        try:
            p = subprocess.run(
                [exe, *cmd[1:]],
                input=payload,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if p.returncode == 0:
            return True
    return False



def composer_url(text: str) -> str:
    """Nodoom prefills the textarea from ?text= (also accepts content/body)."""
    return INTENT_URL + "?" + urllib.parse.urlencode({"text": text}, quote_via=urllib.parse.quote)


def intent_handoff(text: str, redirect_path: Path, *, copy_draft: bool) -> dict:
    """Open /composer?text=… without placing the draft in process argv."""
    copied = bool(copy_draft) and _copy_to_clipboard(text)
    url = composer_url(text)
    # xdg-open exposes its argument through /proc. Put the draft-bearing
    # URL in an owner-only local redirect document; argv contains only
    # its random path.
    escaped = html.escape(url, quote=True)
    document = (
        "<!doctype html><meta charset=utf-8>"
        f'<meta http-equiv="refresh" content="0;url={escaped}">'
        "<title>Opening Nodoom…</title>"
        f'<a href="{escaped}">Continue to Nodoom</a>'
    )
    atomic_write(redirect_path, document.encode("utf-8"), 0o600)
    xdg = shutil.which("xdg-open")
    if xdg is None:
        return {"state": "rejected", "message": "xdg-open not found — cannot open the Nodoom composer"}
    try:
        p = subprocess.Popen(
            [xdg, redirect_path.as_uri()],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as e:
        return {"state": "rejected", "message": f"could not start xdg-open: {e.strerror}"}
    try:
        rc = p.wait(timeout=XDG_OPEN_TIMEOUT)
    except subprocess.TimeoutExpired:
        p.kill()
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            p.wait(timeout=5)
        return {"state": "unknown", "message": "could not confirm the Nodoom composer opened — check your browser"}
    if rc != 0:
        return {"state": "rejected", "message": f"xdg-open failed with exit code {rc}"}
    message = "Composer opened with your draft — review and press Post"
    return {"state": "handoff", "message": message, "copied": copied}


# ----------------------------------------------------------------- commands


def _status_payload(st: dict) -> dict:
    out = {
        "ok": True,
        "jobId": st.get("jobId"),
        "state": st.get("state"),
        "mode": st.get("mode"),
        "message": st.get("message", ""),
    }
    if st.get("url"):
        out["url"] = st["url"]
    if st.get("submittedText") is not None:
        out["submittedText"] = st["submittedText"]
    if isinstance(st.get("draftRevision"), int):
        out["draftRevision"] = st["draftRevision"]
    if st.get("copied") is True:
        out["copied"] = True
    if st.get("pasted") is True:
        out["pasted"] = True
    return out


def cmd_mode() -> int:
    m = compute_mode(load_config())
    out = {
        "ok": True,
        "mode": m["mode"],
        "label": m["label"],
        "paid": m["paid"],
        "copyDraft": m["copyDraft"],
    }
    return emit(out)


def read_stdin_json_object() -> dict:
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        raise BackendError("usage", "expected a JSON object on stdin")
    try:
        obj = json.loads(raw)
    except ValueError:
        raise BackendError("input", "stdin is not valid JSON")
    if not isinstance(obj, dict):
        raise BackendError("input", "stdin must be a JSON object")
    return obj


def _draft_record() -> tuple[str, int]:
    data = read_json(DRAFT_FILE)
    if not isinstance(data, dict):
        return "", 0
    text = data.get("text")
    revision = data.get("revision")
    return (
        text if isinstance(text, str) else "",
        revision if isinstance(revision, int) and revision >= 0 else 0,
    )


def cmd_enqueue() -> int:
    payload = read_stdin_json_object()
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise BackendError("input", "field 'text' must be a non-empty string")
    if len(text) > MAX_TEXT:
        raise BackendError("input", f"post text exceeds Nodoom's {MAX_TEXT}-character limit")
    m = compute_mode(load_config())
    expected_mode = payload.get("expectedMode")
    if expected_mode is not None:
        if expected_mode not in ("intent",):
            raise BackendError("input", "field 'expectedMode' must be intent")
        if expected_mode != m["mode"]:
            raise BackendError(
                "mode-changed",
                "posting mode changed — review the mode label and submit again",
                mode=m["mode"],
                paid=m["paid"],
                label=m["label"],
            )
    rt = ensure_runtime()
    with _global_lock(rt):
        _gc_old_jobs(rt)
        active = _read_active(rt)
        if active:
            st = _load_job(rt, active)
            if st is None:
                _clear_active(rt, active)
            elif st.get("state") not in TERMINAL_STATES:
                raise BackendError("busy", "another post is already in progress", jobId=active)
            else:
                raise BackendError(
                    "busy",
                    "the previous post result is waiting to be observed",
                    jobId=active,
                )
        jid = new_job_id()
        jd = _job_dir(rt, jid)
        jd.mkdir(mode=0o700)
        os.chmod(jd, 0o700)
        now = time.time()
        draft_text, draft_revision = _draft_record()
        job_draft_revision = draft_revision if draft_text == text else None
        input_data = {
            "text": text,
            "mode": m["mode"],
            "copyDraft": m["copyDraft"],
            "created": now,
        }
        if job_draft_revision is not None:
            input_data["draftRevision"] = job_draft_revision
        atomic_write(jd / "input.json", json.dumps(input_data).encode("utf-8"))
        state_fields = {"state": "queued", "mode": m["mode"], "created": now, "message": "queued"}
        if job_draft_revision is not None:
            state_fields["draftRevision"] = job_draft_revision
        _write_state(rt, jid, **state_fields)
        _write_active(rt, jid)
        try:
            logfd = os.open(jd / "worker.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                subprocess.Popen(
                    [sys.executable, str(BACKEND_PATH), "_worker", jid],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=logfd,
                    start_new_session=True,
                    close_fds=True,
                    cwd=str(PLUGIN_DIR),
                )
            finally:
                os.close(logfd)
        except OSError as e:
            _finish_job(rt, jid)
            shutil.rmtree(jd, ignore_errors=True)
            raise BackendError("internal", f"could not start the job worker: {e}") from e
    out = {
        "ok": True,
        "jobId": jid,
        "mode": m["mode"],
        "paid": m["paid"],
        "label": m["label"],
        "copyDraft": m["copyDraft"],
    }
    if job_draft_revision is not None:
        out["draftRevision"] = job_draft_revision
    return emit(out)


def cmd_status(argv: list[str]) -> int:
    jid = require_job_id(argv)
    rt = None
    with contextlib.suppress(BackendError):
        rt = ensure_runtime()
    st = _load_job(rt, jid) if rt else None
    if st is not None:
        st = _reap(rt, jid, st)
        return emit(_status_payload(st))
    mode = None
    with contextlib.suppress(BackendError):
        mode = compute_mode(load_config())["mode"]
    return emit({"ok": True, "jobId": jid, "state": "unknown", "mode": mode, "message": "no such job"})


def cmd_active() -> int:
    rt = None
    with contextlib.suppress(BackendError):
        rt = ensure_runtime()
    if rt is None:
        return emit({"ok": True, "active": None})
    with _global_lock(rt):
        jid = _read_active(rt)
        st = _load_job(rt, jid) if jid else None
        if jid and st is None:
            _clear_active(rt, jid)
            jid = None
        elif jid and st is not None:
            st = _reap(rt, jid, st)
    return emit({"ok": True, "active": _status_payload(st) if (jid and st) else None})


def cmd_ack(argv: list[str]) -> int:
    jid = require_job_id(argv)
    rt = None
    with contextlib.suppress(BackendError):
        rt = ensure_runtime()
    note = None
    if rt is not None:
        st = _load_job(rt, jid)
        if st is not None:
            st = _reap(rt, jid, st)
            if st.get("state") not in TERMINAL_STATES:
                raise BackendError("busy", "job is still in progress; ack after it finishes", jobId=jid)
        else:
            note = "job not found (nothing to acknowledge)"
        with _global_lock(rt):
            _finish_job(rt, jid)
    out = {"ok": True, "jobId": jid}
    if note:
        out["note"] = note
    return emit(out)


@contextlib.contextmanager
def _draft_lock():
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(CONFIG_DIR / "draft.lock", flags, 0o600)
    except OSError as e:
        raise BackendError("config", f"cannot open the draft lock: {e.strerror}") from e
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def cmd_draft(argv: list[str]) -> int:
    sub = argv[0] if argv else ""
    ensure_config_files()
    if sub == "get":
        text, revision = _draft_record()
        out = {"ok": True, "text": text, "revision": revision}
        data = read_json(DRAFT_FILE)
        if data is None and DRAFT_FILE.exists():
            out["warning"] = "draft.json is unreadable or corrupt; returning empty text"
        return emit(out)
    if sub == "set":
        if len(argv) != 1:
            raise BackendError("usage", "expected: draft set")
        payload = read_stdin_json_object()
        next_text = payload.get("text")
        if not isinstance(next_text, str):
            raise BackendError("input", "field 'text' must be a string")
        if len(next_text) > MAX_TEXT:
            raise BackendError("input", f"post text exceeds Nodoom's {MAX_TEXT}-character limit")
        with _draft_lock():
            _, revision = _draft_record()
            next_revision = revision + 1
            atomic_write(
                DRAFT_FILE,
                json.dumps({"text": next_text, "revision": next_revision, "updated": time.time()}).encode("utf-8"),
            )
        return emit({"ok": True, "revision": next_revision})
    if sub == "clear":
        if len(argv) > 2:
            raise BackendError("usage", "expected: draft clear [EXPECTED_REVISION]")
        expected = None
        if len(argv) == 2:
            try:
                expected = int(argv[1])
            except ValueError as e:
                raise BackendError("usage", "draft clear expected revision must be an integer") from e
        with _draft_lock():
            _, revision = _draft_record()
            if expected is not None and expected != revision:
                raise BackendError(
                    "conflict",
                    "saved draft changed after submission — preserving it",
                    revision=revision,
                )
            next_revision = revision + 1
            atomic_write(
                DRAFT_FILE,
                json.dumps({"text": "", "revision": next_revision, "updated": time.time()}).encode("utf-8"),
            )
        return emit({"ok": True, "revision": next_revision})
    raise BackendError("usage", "expected: draft get | draft set | draft clear [EXPECTED_REVISION]")


# ------------------------------------------------------------------- worker


def _claim_worker(jd: Path) -> None:
    """Atomically ensure exactly one worker can execute a queued post."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(jd / "worker.json", flags, 0o600)
    except FileExistsError as e:
        raise BackendError("busy", "this job already has a worker") from e
    except OSError as e:
        raise BackendError("internal", f"cannot claim the job worker: {e.strerror}") from e
    try:
        os.fchmod(fd, 0o600)
        payload = json.dumps({"pid": os.getpid(), "started": time.time()}).encode("utf-8")
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _run_worker(jid: str) -> int:
    rt = ensure_runtime()
    jd = _job_dir(rt, jid)
    _claim_worker(jd)
    inp = read_json(jd / "input.json")
    if not isinstance(inp, dict) or not isinstance(inp.get("text"), str):
        _write_state(rt, jid, state="unknown", message="job input missing — result unknown; draft kept")
        return emit({"ok": False, "jobId": jid, "state": "unknown"})
    text, mode = inp["text"], inp.get("mode", "intent")
    copy_draft = inp.get("copyDraft", True)
    if not isinstance(copy_draft, bool):
        copy_draft = True
    draft_revision = inp.get("draftRevision")
    _log(f"worker start job={jid} mode={mode} len={len(text)}")
    _write_state(rt, jid, state="running", message="opening the Nodoom composer")
    result = intent_handoff(text, jd / "intent.html", copy_draft=copy_draft)
    _log(f"worker done job={jid} state={result['state']}")
    fields = {
        "state": result["state"],
        "message": result.get("message", ""),
        "submittedText": text,
    }
    if isinstance(draft_revision, int):
        fields["draftRevision"] = draft_revision
    if result.get("url"):
        fields["url"] = result["url"]
    if result.get("copied") is True:
        fields["copied"] = True
    st = _write_state(rt, jid, **fields)
    return emit(_status_payload(st))


def cmd_worker(argv: list[str]) -> int:
    jid = require_job_id(argv)
    try:
        return _run_worker(jid)
    except BackendError as e:
        _log(f"worker aborted job={jid}: {e.kind}: {e.message}")
        # A duplicate worker must not overwrite the legitimate worker's state.
        if e.kind != "busy":
            with contextlib.suppress(Exception):
                rt = ensure_runtime()
                _write_state(rt, jid, state="unknown", message=f"worker aborted ({e.message}); result unknown — draft kept")
        return emit({"ok": False, "jobId": jid, "kind": e.kind, "message": e.message})
    except Exception as e:
        _log(f"worker crashed job={jid}: {type(e).__name__}")
        with contextlib.suppress(Exception):
            rt = ensure_runtime()
            _write_state(rt, jid, state="unknown", message="worker crashed — result unknown; draft kept")
        return emit({"ok": False, "jobId": jid, "state": "unknown", "message": "worker crashed"})


# --------------------------------------------------------------------- main


USAGE = "usage: backend.py mode | enqueue | status JOBID | active | ack JOBID | draft get|set|clear"


def main(argv: list[str]) -> int:
    if not argv:
        return emit({"ok": False, "kind": "usage", "message": USAGE}, 2)
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "mode":
            return cmd_mode()
        if cmd == "enqueue":
            return cmd_enqueue()
        if cmd == "status":
            return cmd_status(rest)
        if cmd == "active":
            return cmd_active()
        if cmd == "ack":
            return cmd_ack(rest)
        if cmd == "draft":
            return cmd_draft(rest)
        if cmd == "_worker":
            return cmd_worker(rest)
        return emit({"ok": False, "kind": "usage", "message": f"unknown command {cmd!r}; {USAGE}"}, 2)
    except BackendError as e:
        out = {"ok": False, "kind": e.kind, "message": e.message}
        out.update(e.extra)
        return emit(out, 1)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        os._exit(1)
