#!/usr/bin/env python3
"""Stall detector for long, expensive jobs. Stdlib only, Python 3.8+.

WHY THIS EXISTS. Three times now a job has gone quiet and the quiet was
misread. A `modal run` hung after a remote failure and a watcher sat on it for
~69 minutes because the process was still alive. A data-generation run went 15
minutes without completing a single API call while holding open sockets, and
looked identical to one that was merely slow. In both cases the operator had no
signal at all until they went looking.

Three lessons are baked in here, because all three were learned the expensive
way:

  1. A LIVE PROCESS IS NOT PROGRESS. `pgrep` says a hung job and a working job
     are the same thing. So this never treats liveness as health -- it watches a
     PROBE that counts real work done, and liveness only decides when to stop.
     A *defunct* (zombie) process is not alive either, and is checked for.

  2. THE JOB'S OWN LOG IS NOT A PROBE. Python buffers stdout when it is not a
     tty, so a redirected run writes zero bytes until it exits. A watcher that
     tails the log sees nothing for an hour and cannot tell that from a crash.
     The probe must be something the job touches as a side effect of working --
     a growing cache directory, an output file's line count, rows in a table.

  3. MOVEMENT IS NOT PROGRESS EITHER. A value that flickers 41, 42, 41, 42 --
     a temp file written and unlinked by a retry loop -- is a job going nowhere.
     Only movement AWAY from the high-water mark resets the stall timer.

SILENCE IS NEVER SUCCESS HERE, AND IT IS ENFORCED. Every terminal state emits a
line -- stall, recovery, exit, probe failure, being signalled, even an
unexpected crash of the watcher itself. On top of that a HEARTBEAT guarantees a
line at least every --heartbeat-seconds no matter what. So silence for longer
than that means the watcher is DEAD, and never means the job is fine.

USAGE -- designed to be driven by the Monitor tool, one stdout line per event:

    python3 watch_job.py --label "v2 datagen" \\
        --probe "find some/cache -type f | wc -l" \\
        --pid 12345 --stall-seconds 300

    # watch until the probe reaches a target
    python3 watch_job.py --label eval \\
        --probe "wc -l < out/rows.jsonl" --target 147

Run --help for the probe rules, the event vocabulary and the exit codes.

THE EVENT BUDGET, AND WHY IT IS THE WAY IT IS. Two requirements pull against
each other: a stall must be re-announced (one notification at minute 5 of a
twelve-hour failure is indistinguishable from calm), yet a driver that consumes
these lines stops a source producing too many events -- and being stopped makes
this watcher silent, the exact failure it exists to prevent. The resolution is
NOT to pick a side. Repetition is kept but put on exponential backoff, and
every unbounded source is capped: the log starts at EOF and emits at most a few
lines per poll, and probe-health events share one rate-limited channel. Twelve
hours of total failure costs about fifty lines instead of several thousand,
and no single poll can burst.

DOCUMENTED LIMITATIONS -- deliberate, not oversights:

  * The exit STATUS of a non-child process is not observable. EXIT reports that
    the pid is gone and refuses to call that success. Check the job's own log.
  * A pid can be recycled by the OS. A watch long enough for the kernel to wrap
    its pid space could in principle end up watching a stranger. Nothing
    portable fixes this; --probe is the real signal, --pid only ends the watch.
  * The probe is a shell command and runs with this process's privileges. It is
    operator-supplied code; this script does not sandbox it. It DOES bound it:
    every probe runs in its own session, its whole process group is killed on
    timeout, and its output is capped -- so a slow or runaway probe can neither
    accumulate orphans nor exhaust memory.
  * --log is a SECOND source of truth, not the primary one. It is a heuristic
    regex scan; it will miss failure text it does not recognise, and it reports
    only what is written AFTER the watch starts unless --log-from-start is
    given. A quiet log is not evidence of health -- that is what the probe is
    for.
  * Probe-health and stall reminders are never re-emitted more often than once
    a minute, even if --stall-seconds is set lower. Below that the events cost
    more than they inform. The first report of anything is always immediate.
  * --probe-direction any remembers the last 256 distinct values, so a probe
    cycling through more than 256 states can still look like progress. Bounded
    memory was preferred to an unbounded set in a process that runs for days.
  * --max-seconds 0 really does mean forever. It is the one way to get an
    unbounded watcher, and it has to be asked for by hand; the default is 24h
    so that a watch started in some project and forgotten cannot outlive the
    machine's uptime.
"""
from __future__ import annotations

import argparse
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from collections import OrderedDict
from pathlib import Path

__all__ = ["emit", "probe_value", "pid_alive", "scan_log", "LogScanner", "main"]

# --------------------------------------------------------------------- limits
#: Never read more than this much of --log per poll. A watcher that ingests a
#: 10GB log in one gulp is a machine-exhaustion bug wearing a monitoring hat.
LOG_READ_BUDGET = 4 << 20          # 4 MiB per poll
LOG_CHUNK = 1 << 18                # 256 KiB per read() call
#: Cap LOGFAIL events per poll. Monitor stops a source that produces too many
#: events, and a stopped watcher is a silent watcher.
LOG_EVENTS_PER_POLL = 5
LOG_DEDUP_MEMORY = 512             # remember this many reported lines
MAX_EVENT_CHARS = 400
#: A real count never has this many characters. Refusing longer text also side-
#: steps CPython's 4300-digit int() conversion limit, which is a hard error.
MAX_PROBE_NUMBER_CHARS = 64
#: Hard cap on what a probe may print. subprocess.communicate() has no
#: limit, so `--probe "cat out.jsonl"` (a plausible slip for `wc -l <`)
#: would pull the whole file into the watcher's RAM.
MAX_PROBE_OUTPUT = 1 << 16      # 64 KiB

#: Regexes that mean a job died, for the optional --log scan.
#:
#: These are ANCHORED on purpose. The previous substring form matched "Error"
#: inside "train_error=0.003", "OOM" inside "Zooming", and "429" inside
#: "step 4290/10000" -- a 100% false-positive rate on ordinary healthy output.
#: A watcher that cries wolf on every line teaches its reader to ignore
#: LOGFAIL, and then the one real Traceback is ignored too.
FAILURE_PATTERNS = (
    re.compile(r"^\s*Traceback \(most recent call last\)"),
    re.compile(r"^\s*[\w.]*(?:Error|Exception)\b\s*:"),
    re.compile(r"\b(?:MemoryError|OutOfMemoryError|RateLimitError)\b"),
    re.compile(r"\bCUDA out of memory\b", re.I),
    re.compile(r"\bout of memory\b", re.I),
    re.compile(r"\bOOM(?:-?kill(?:ed|er)?)?\b"),
    re.compile(r"\bKilled\b(?!\s+0\b)"),
    re.compile(r"\bFAILED\b"),
    re.compile(r"\b(?:429|5\d{2})\b(?=.*\b(?:status|HTTP|error|Too Many|"
               r"rate.?limit|quota|retry)\b)", re.I),
    re.compile(r"\bquota (?:exceeded|exhausted)\b", re.I),
    re.compile(r"\bfatal\b\s*:", re.I),
)

EXIT_OK = 0
EXIT_TIMEOUT = 1
EXIT_USAGE = 2
EXIT_TARGET_PRESET = 3
EXIT_PROBE_DEAD = 4
EXIT_WATCHER_DIED = 5
EXIT_SIGNALLED = 130


class _Stopped(Exception):
    """Raised from a signal handler so the watcher can announce its own death."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


# ---------------------------------------------------------------------- emit
def _one_line(text: object, limit: int = MAX_EVENT_CHARS) -> str:
    """Collapse anything to a single line, because one event is one line.

    A newline in a --label or in a probe's traceback used to SPLIT one event
    into several, and the extra lines' first tokens became fabricated event
    kinds -- a probe's stderr could forge a clean `EXIT ... all good`. Sanitise
    at the choke point so no caller can do that by accident or on purpose.
    """
    flat = " ".join(str(text).split())
    return flat[:limit - 1] + "…" if len(flat) > limit else flat


#: When the last event was printed, monotonic. The heartbeat reads this, so it
#: has to count EVERY event -- including LOGFAIL emitted from the log scanner.
_LAST_EMIT = [0.0]


def emit(kind: str, label: str, message: str) -> None:
    """One event, one line, flushed. Monitor turns each line into a
    notification, so an unflushed line is an event that never happened."""
    print("{:10s} [{}] {}".format(_one_line(kind, 24), _one_line(label, 80),
                                  _one_line(message)), flush=True)
    _LAST_EMIT[0] = time.monotonic()


def _fmt_secs(seconds: float) -> str:
    """Human duration that never rounds a real interval away to '0s'."""
    if seconds < 10:
        return "{:.3g}s".format(seconds)
    if seconds < 90:
        return "{:.0f}s".format(seconds)
    if seconds < 5400:
        return "{:.0f}m{:02.0f}s".format(seconds // 60, seconds % 60)
    return "{:.0f}h{:02.0f}m".format(seconds // 3600, (seconds % 3600) // 60)


def _fmt_num(value) -> str:
    return "{:g}".format(value) if isinstance(value, float) else str(value)


# --------------------------------------------------------------------- probe
def _shell_argv(cmd: str):
    """Prefer bash with pipefail so a pipeline cannot hide its producer's error.

    `find missing/dir -type f | wc -l` exits 0 and prints 0 under /bin/sh: the
    pipeline's status is `wc`'s. That turns a completely blind watcher into one
    that looks correctly armed and merely idle -- the single most likely
    misconfiguration when an agent writes the probe for a project it does not
    know.
    """
    bash = shutil.which("bash") or ("/bin/bash" if os.path.exists("/bin/bash")
                                    else None)
    if bash:
        return [bash, "-o", "pipefail", "-c", cmd], True
    return ["/bin/sh", "-c", cmd], False


def _terminate_group(proc) -> None:
    """Kill the probe's whole process GROUP, and only ever that group.

    subprocess's own timeout kills the direct /bin/sh child only; every
    grandchild a pipeline forked survives, is reparented to pid 1 and keeps
    running -- one leaked process per timed-out poll, forever.

    This is the ONLY place in this file that delivers a real signal, so the
    target is checked three ways before anything is sent: the group must be
    positive, must be a group WE created (start_new_session makes the child its
    own leader, so pgid == pid), and must not be our own group. A caller can
    never steer this at an unrelated process.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    ours = os.getpgid(0)
    safe = pgid is not None and pgid > 0 and pgid == proc.pid and pgid != ours
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            break
        try:
            if safe:
                os.killpg(pgid, sig)
            else:                      # never guessed at: fall back to the child
                proc.send_signal(sig)
        except OSError:
            break
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            continue
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _read_bounded(proc, timeout, limit):
    """Drain the probe's output under BOTH a deadline and a byte cap.

    subprocess.communicate() has no size limit, so a probe like
    `cat out.jsonl` -- a very plausible slip when the intent was
    `wc -l < out.jsonl` -- pulls the whole file into the watcher's RAM. A
    watcher must never be the thing that exhausts the machine.

    Output past the cap is read and DISCARDED rather than left unread, so the
    child cannot wedge on a full pipe and turn a bounded read into a hang.

    Returns (stdout, stderr, timed_out, truncated).
    """
    sel = selectors.DefaultSelector()
    bufs = {"out": bytearray(), "err": bytearray()}
    truncated = False
    try:
        for name, stream in (("out", proc.stdout), ("err", proc.stderr)):
            os.set_blocking(stream.fileno(), False)
            sel.register(stream.fileno(), selectors.EVENT_READ, name)
        live = 2
        deadline = time.monotonic() + timeout
        while live > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, None, True, truncated
            for key, _ in sel.select(timeout=min(0.2, remaining)):
                try:
                    data = os.read(key.fd, 65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    data = b""
                if not data:
                    sel.unregister(key.fd)
                    live -= 1
                    continue
                buf = bufs[key.data]
                if len(buf) < limit:
                    buf += data[:limit - len(buf)]
                else:
                    truncated = True
    finally:
        sel.close()
    return bytes(bufs["out"]), bytes(bufs["err"]), False, truncated


_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_NUMBER_TOKEN = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def _coerce(raw: str):
    if len(raw) > MAX_PROBE_NUMBER_CHARS:
        return None
    try:
        number = float(raw)
    except (ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return int(number) if number.is_integer() and abs(number) < 2 ** 53 else number


def probe_value(cmd: str, timeout: float = 60.0, parse: str = "strict"):
    """Run the probe and return (number, None), or (None, reason).

    A probe that errors is reported rather than swallowed: a watcher whose probe
    silently returns nothing looks exactly like a job making no progress, and
    the operator would chase the wrong problem.

    Parsing is STRICT by default: the output must BE a number, not merely
    contain one. Taking the first digit run anywhere in the text read the YEAR
    out of `2026-08-21 ... done=3`, read `-3` out of a `sh-3.2$` prompt, read
    `1` out of `1,234,567`, and read `0` out of `found 0 files in 3 dirs`. With
    --target that produced an instant, confident, false TARGET and exit 0.
    """
    argv, pipefail = _shell_argv(cmd)
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError as exc:
        return None, "probe could not be started: {}".format(exc)

    try:
        out, err, timed_out, truncated = _read_bounded(proc, timeout,
                                                       MAX_PROBE_OUTPUT)
        if timed_out:
            return None, ("probe timed out after {} (its process group was "
                          "killed)".format(_fmt_secs(timeout)))
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return None, ("probe closed its output but did not exit within 5s "
                          "(its process group was killed)")
        if truncated:
            return None, ("probe printed more than {} bytes -- it must print "
                          "ONE number, not a file. Output was discarded."
                          .format(MAX_PROBE_OUTPUT))
    finally:
        if proc.poll() is None:
            _terminate_group(proc)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass

    stdout = out.decode("utf-8", "replace") if out else ""
    stderr = err.decode("utf-8", "replace") if err else ""
    if proc.returncode != 0:
        detail = _one_line(stderr, 200) or "(no stderr)"
        hint = "" if pipefail else " [no bash: a pipeline's error may be hidden]"
        return None, "probe exited {}: {}{}".format(proc.returncode, detail, hint)

    text = stdout.strip()
    if parse == "last":
        tokens = _NUMBER_TOKEN.findall(text)
        value = _coerce(tokens[-1]) if tokens else None
    else:
        value = _coerce(text) if _NUMBER.match(text) else None

    if value is None:
        return None, ("probe printed no number: {!r} -- the probe must print "
                      "ONE number and nothing else (see --probe-parse)"
                      .format(_one_line(text, 120) or ""))
    return value, None


# ------------------------------------------------------------------ liveness
def _is_defunct(pid: int) -> bool:
    """True if the pid exists but has already exited and not been reaped.

    os.kill(pid, 0) succeeds for a ZOMBIE, so the watcher reported
    `process=alive` for a corpse and never emitted EXIT -- which is precisely
    the founding incident this file exists to prevent. A job whose parent never
    reaps it stays defunct forever, so this is not a narrow race.
    """
    try:                                            # Linux: no fork needed
        with open("/proc/{}/stat".format(pid), "rb") as fh:
            data = fh.read()
        return data.rsplit(b")", 1)[-1].split()[0:1] == [b"Z"]
    except OSError:
        pass
    except Exception:
        return False
    ps = shutil.which("ps")
    if not ps:
        return False                                # cannot tell; assume alive
    try:
        out = subprocess.run([ps, "-o", "state=", "-p", str(pid)],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.stdout.decode("ascii", "replace").strip().startswith("Z")


def pid_alive(pid: int) -> bool:
    """Does this pid name a process that is still running?

    Hard precondition: pid > 0. On POSIX, os.kill(0, sig) targets the CALLER'S
    ENTIRE PROCESS GROUP and os.kill(-1, sig) targets every process the user may
    signal. With signal 0 that is only an existence probe -- both always
    "succeed", which made the watch immortal -- but this function must never be
    reachable with a group target, because the next person to put a real signal
    here would turn `--pid 0` into a process-group kill.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("pid must be a positive process id, got {!r}".format(pid))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                 # exists, owned by someone else
        return True
    except (OverflowError, ValueError):     # pid larger than the C pid_t
        return False
    except OSError:
        return True                         # unexpected errno: do not claim death
    return not _is_defunct(pid)


# ------------------------------------------------------------------- the log
class LogScanner:
    """Bounded, rotation-aware scan of a log for failure signatures."""

    def __init__(self, path: Path, offset: int = 0, label: str = "-"):
        self.path = Path(path)
        self.offset = offset
        self.label = label
        self.key = None                       # (st_dev, st_ino, head fingerprint)
        self._seen = OrderedDict()

    # -- identity ----------------------------------------------------------
    def _head(self, fh) -> bytes:
        try:
            here = fh.tell()
            fh.seek(0)
            head = fh.read(64)
            fh.seek(here)
            return head
        except OSError:
            return b""

    @staticmethod
    def _same_file(old, new) -> bool:
        """Same inode AND a compatible prefix.

        The prefix must be compared with startswith, not ==: a log that was
        only 10 bytes long at the first poll has a 10-byte fingerprint, and
        would otherwise look 'replaced' the moment it grew past 64 bytes.
        """
        if old[:2] != new[:2]:
            return False
        a, b = old[2], new[2]
        return a.startswith(b) or b.startswith(a)

    def _remember(self, line: str) -> bool:
        """True the first time a given line is seen; suppresses exact repeats."""
        if line in self._seen:
            self._seen.move_to_end(line)
            return False
        self._seen[line] = None
        while len(self._seen) > LOG_DEDUP_MEMORY:
            self._seen.popitem(last=False)
        return True

    def scan(self) -> int:
        """Emit new failure lines. Returns the new offset. Never raises."""
        try:
            st = self.path.stat()
        except OSError:
            return self.offset
        if not os.path.isfile(str(self.path)):
            return self.offset                 # a directory is not a log
        try:
            fh = open(str(self.path), "rb")
        except OSError:
            return self.offset

        emitted = suppressed = 0

        def consider(raw: bytes):
            """Report one candidate line. Returns nothing; updates counters."""
            nonlocal emitted, suppressed
            line = raw.decode("utf-8", "replace").rstrip("\r").strip()
            if not line or not any(p.search(line) for p in FAILURE_PATTERNS):
                return
            if emitted >= LOG_EVENTS_PER_POLL:
                suppressed += 1
                return
            if self._remember(line):
                emit("LOGFAIL", self.label, line)
                emitted += 1

        try:
            head = self._head(fh)
            key = (st.st_dev, st.st_ino, head)
            if self.key is not None and not self._same_file(self.key, key):
                # Rotated, or truncated and regrown. The old offset points into
                # a DIFFERENT file; seeking to it skips everything before it --
                # including the crash that caused the rotation. The old guard
                # only noticed when the replacement was SMALLER than the offset.
                emit("LOGROTATE", self.label,
                     "log was replaced (new inode or new first bytes); "
                     "rescanning it from the start so a crash written before "
                     "the rotation is not lost")
                self.offset = 0
                self._seen.clear()
            elif st.st_size < self.offset:
                emit("LOGROTATE", self.label,
                     "log was truncated; rescanning it from the start")
                self.offset = 0
                self._seen.clear()
            self.key = key

            if st.st_size <= self.offset:
                return self.offset

            fh.seek(self.offset)
            budget = LOG_READ_BUDGET
            pending = b""
            consumed = self.offset
            while budget > 0:
                chunk = fh.read(min(LOG_CHUNK, budget))
                if not chunk:
                    # EOF. A trailing line with no newline may still be being
                    # written, so scan it but do NOT consume it -- the dedup
                    # memory stops it being reported twice when it completes.
                    if pending:
                        consider(pending)
                    break
                budget -= len(chunk)
                pending += chunk
                lines = pending.split(b"\n")
                pending = lines.pop()           # keep the partial last line
                for raw in lines:
                    consider(raw)
                    consumed += len(raw) + 1
            self.offset = consumed
        except OSError:
            return self.offset
        finally:
            fh.close()

        if suppressed:
            emit("LOGFAIL", self.label,
                 "... and {} more matching lines this poll (suppressed; the log "
                 "is the second source of truth, check it directly)".format(suppressed))
        return self.offset


#: Scanners kept alive per path so the plain scan_log() helper is rotation-safe
#: too. Bounded, so a long-lived process cannot grow this without limit.
_SCANNERS = OrderedDict()
_SCANNER_MEMORY = 32


def scan_log(path, offset: int, label: str) -> int:
    """Scan a log for failure signatures. Returns the new offset.

    REMEMBERS the file's identity between calls for the same path. It has to:
    rotation can only be detected by comparing against what the file was last
    time, and a caller that re-created a scanner on every poll would seek
    straight into the replacement file and silently skip everything before the
    old offset -- including the crash that caused the rotation.
    """
    key = str(path)
    scanner = _SCANNERS.get(key)
    if scanner is None:
        scanner = LogScanner(Path(path), offset, label)
        _SCANNERS[key] = scanner
        while len(_SCANNERS) > _SCANNER_MEMORY:
            _SCANNERS.popitem(last=False)
    else:
        _SCANNERS.move_to_end(key)
        scanner.path = Path(path)
        scanner.offset = offset
        scanner.label = label
    return scanner.scan()


# --------------------------------------------------------------------- main
EPILOG = """\
THE PROBE IS THE WHOLE GAME
  --probe must print ONE number and NOTHING else, and that number must GROW as
  real work is done. Good probes count a side effect of working:
      wc -l < out/rows.jsonl          rows written so far
      find some/cache -type f | wc -l files produced so far
      sqlite3 db 'select count(*) from t'

  DO NOT use the job's own log as the probe. Python buffers stdout when it is
  not a tty, so a redirected run writes zero bytes until it exits.
  DO NOT use `du -sh` or anything printing units ("1.2G") -- it is not a number.
  DO NOT rely on a pipeline's exit status: `find missing/ | wc -l` exits 0 and
  prints 0. This script runs the probe under `bash -o pipefail` so the producer's
  failure surfaces as PROBE-FAIL instead of a confident, wrong 0.
  A probe printing anything else ("found 3 files") is a PROBE-FAIL, not a number
  to guess at. Use --probe-parse last only if you knowingly want the last number.

EVENTS (one per line; first token is the kind)
  START       the watch is armed. Printed once, so a quiet watcher is visible.
  PROGRESS    the probe advanced. Throttled by --progress-every.
  DROP        the probe moved BACKWARD. Informative, but not progress: it does
              not reset the stall timer.
  STALL       no forward movement for --stall-seconds. THE event this exists for.
              Re-announced on a widening backoff for as long as it lasts.
  RECOVER     a stall resolved on its own.
  ALIVE       heartbeat. Nothing else needed saying, and the watcher is well.
  EXIT        the watched pid is gone. Says nothing about SUCCESS -- unknowable.
  TARGET      the probe reached --target during the watch.
  TARGET-PRESET  --target was ALREADY satisfied at START; refused as ambiguous.
  PROBE-FAIL  the probe itself broke. Never read as calm.
  PROBE-OK    a broken probe started working again.
  PROBE-DEAD  the probe failed --probe-fail-limit times running; giving up.
  LOGFAIL     a failure signature in --log. Second source of truth, heuristic.
  LOGROTATE   --log was rotated or truncated; rescanning it from the start.
  TIMEOUT     --max-seconds elapsed.
  STOPPED     this watcher was signalled. The job is NOT watched after this.
  WATCHER-DIED  an unexpected error in the watcher. Never fail silently.

EXIT CODES
  0  TARGET reached, or the watched pid exited
  1  TIMEOUT (--max-seconds elapsed)
  2  bad arguments
  3  --target was already satisfied before the watch began (nothing observed)
  4  PROBE-DEAD (the probe never recovered)
  5  the watcher itself hit an unexpected error
  130 the watcher was signalled (SIGINT/SIGTERM/SIGHUP)

SILENCE MEANS THE WATCHER IS DEAD. It never means the job is fine. A healthy
watch prints at least one line every --heartbeat-seconds.
"""


def _positive(name, value, allow_zero=False):
    if value is None:
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return "{} must be a finite number".format(name)
    if allow_zero:
        return None if value >= 0 else "{} must be >= 0".format(name)
    return None if value > 0 else "{} must be positive".format(name)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="watch_job.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=("Watch a long job and BREAK THE SILENCE when it stalls, "
                     "dies, or finishes.\nOne event per stdout line, designed "
                     "for the Monitor tool."),
        epilog=EPILOG)
    ap.add_argument("--label", required=True,
                    help="name shown in every event (e.g. the job or project)")
    ap.add_argument("--probe", required=True,
                    help="shell command printing ONE number that GROWS as work "
                         "is done; see the notes below")
    ap.add_argument("--probe-parse", choices=("strict", "last"), default="strict",
                    help="strict (default): the output must BE a number. "
                         "last: take the last number found in it (opt-in; "
                         "guessing is how a date becomes a probe value)")
    ap.add_argument("--probe-direction", choices=("up", "down", "any"), default="up",
                    help="which way real progress moves the probe. up (default): "
                         "only a NEW HIGH counts. down: only a new low (queue "
                         "depth, items remaining). any: any value not seen "
                         "recently -- flicker between two values is not progress")
    ap.add_argument("--probe-timeout", type=float, default=60.0,
                    help="kill the probe's process group after this long "
                         "(default 60)")
    ap.add_argument("--probe-fail-limit", type=int, default=20,
                    help="consecutive probe failures before PROBE-DEAD and exit "
                         "4; a watcher blind this long is not watching (default "
                         "20, 0 disables)")
    ap.add_argument("--pid", type=int, default=None,
                    help="watched process; its disappearance ends the watch. "
                         "Must be a positive pid")
    ap.add_argument("--stall-seconds", type=float, default=300.0,
                    help="no forward probe movement for this long -> STALL "
                         "(default 300)")
    ap.add_argument("--poll-seconds", type=float, default=30.0,
                    help="how often to read the probe (default 30)")
    ap.add_argument("--progress-every", type=float, default=300.0,
                    help="throttle for PROGRESS/DROP events (default 300s)")
    ap.add_argument("--heartbeat-seconds", type=float, default=900.0,
                    help="emit ALIVE if nothing else was said for this long, so "
                         "silence always means the WATCHER died (default 900, "
                         "0 disables)")
    ap.add_argument("--target", type=float, default=None,
                    help="probe value at which to emit TARGET and exit 0")
    ap.add_argument("--target-allow-preexisting", action="store_true",
                    help="accept a --target that is already satisfied at START "
                         "(by default that is refused as exit 3: the watcher "
                         "saw no work and must not certify success)")
    ap.add_argument("--log", default=None,
                    help="optional log FILE to scan for failure signatures "
                         "(second source of truth, not the probe)")
    ap.add_argument("--log-from-start", action="store_true",
                    help="also scan --log's existing content; by default the "
                         "watch reports what happens FROM NOW, so an old log is "
                         "not replayed as a flood of stale alarms")
    ap.add_argument("--max-seconds", type=float, default=86400.0,
                    help="give up after this long (default 86400 = 24h). Use 0 "
                         "for no limit -- but an unbounded watcher outlives the "
                         "session that started it")
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    problems = []
    for name, value in (("--poll-seconds", args.poll_seconds),
                        ("--stall-seconds", args.stall_seconds),
                        ("--probe-timeout", args.probe_timeout)):
        problems.append(_positive(name, value))
    for name, value in (("--progress-every", args.progress_every),
                        ("--heartbeat-seconds", args.heartbeat_seconds),
                        ("--max-seconds", args.max_seconds)):
        problems.append(_positive(name, value, allow_zero=True))
    if args.pid is not None and args.pid <= 0:
        problems.append("--pid must be a positive process id (0 means 'this "
                        "process group' and -1 means 'every process' to the "
                        "kernel; neither is a job)")
    if args.probe_fail_limit < 0:
        problems.append("--probe-fail-limit must be >= 0")
    if not _one_line(args.label):
        problems.append("--label must not be empty")
    problems = [p for p in problems if p]
    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        return EXIT_USAGE

    log_path = None
    if args.log:
        log_path = Path(args.log)
        if log_path.exists() and not log_path.is_file():
            print("--log must be a regular file, not a directory: {}".format(
                args.log), file=sys.stderr)
            return EXIT_USAGE

    scanner = None
    if log_path is not None:
        start_offset = 0
        if not args.log_from_start:
            try:
                start_offset = log_path.stat().st_size
            except OSError:
                start_offset = 0
        scanner = LogScanner(log_path, start_offset, args.label)

    def say(kind, message):
        emit(kind, args.label, message)

    def alive_word():
        if args.pid is None:
            return "unknown"
        return "alive" if pid_alive(args.pid) else "GONE"

    # All timing is MONOTONIC. Wall clock is adjustable: a closed laptop lid or
    # an NTP step used to fabricate an 8-hour STALL on a healthy job and make
    # --max-seconds fire hours early (or late).
    started = time.monotonic()
    _LAST_EMIT[0] = started

    value, err = probe_value(args.probe, args.probe_timeout, args.probe_parse)
    if err:
        say("PROBE-FAIL", "{} (at start)".format(err))
    start_value = value
    last_value = value
    mark = value                       # high-water (or low-water) mark
    seen = OrderedDict()
    if value is not None:
        seen[value] = None
    last_move = started
    last_progress_emit = started
    stalled = False
    stall_since = None
    stall_interval = 0.0
    next_stall_report = 0.0
    fail_streak = 0
    fail_message = err
    last_err_state = bool(err)
    health_suppressed = 0
    health_interval = max(args.poll_seconds, 60.0)
    # If the start probe already failed we have just reported it; otherwise let
    # the first real failure be reported the instant it happens.
    next_health_report = started + health_interval if err else started - 1.0

    say("START", "probe={} stall_after={} poll={} direction={}{}{}{}".format(
        _fmt_num(value) if value is not None else "n/a",
        _fmt_secs(args.stall_seconds), _fmt_secs(args.poll_seconds),
        args.probe_direction,
        " pid={}".format(args.pid) if args.pid is not None else "",
        " target={}".format(_fmt_num(args.target)) if args.target is not None else "",
        " max={}".format(_fmt_secs(args.max_seconds)) if args.max_seconds else " max=none"))

    if args.target is not None and value is not None and _hit(value, args.target,
                                                              args.probe_direction):
        if not args.target_allow_preexisting:
            say("TARGET-PRESET",
                "--target {} was ALREADY satisfied by the very first probe read "
                "({}). This watcher observed NO work: the value may be left over "
                "from a previous run. Refusing to report success. Delete the "
                "stale output, or pass --target-allow-preexisting if this is "
                "genuinely already done.".format(_fmt_num(args.target),
                                                 _fmt_num(value)))
            return EXIT_TARGET_PRESET
        say("TARGET", "probe={} already met target {} before the watch began "
                      "(--target-allow-preexisting)".format(
                          _fmt_num(value), _fmt_num(args.target)))
        return EXIT_OK

    while True:
        time.sleep(args.poll_seconds)
        now = time.monotonic()

        if scanner is not None:
            scanner.scan()

        value, err = probe_value(args.probe, args.probe_timeout, args.probe_parse)

        # PROBE-FAIL and PROBE-OK share ONE rate limit with exponential backoff.
        # Throttling only the failures leaves a probe that FLAPS -- fail, ok,
        # fail, ok -- emitting two events every poll forever, which is the same
        # flood by another route. Flapping is a real shape: an output file
        # rewritten atomically, or a resource that is intermittently there.
        if err:
            fail_streak += 1
        recovered = fail_streak if (not err and fail_streak) else 0
        health_changed = (bool(err) != last_err_state
                          or (err is not None and err != fail_message))

        if err is not None or health_changed:
            if now >= next_health_report:
                extra = (" ({} further probe-health changes suppressed)".format(
                    health_suppressed) if health_suppressed else "")
                if err is not None:
                    say("PROBE-FAIL", "{} [{} in a row]{}".format(
                        err, fail_streak, extra))
                elif recovered or health_suppressed:
                    say("PROBE-OK", "the probe recovered after {} consecutive "
                                    "failures; probe={}{}".format(
                                        recovered, _fmt_num(value), extra))
                health_suppressed = 0
                health_interval = min(health_interval * 2, 1800.0)
                next_health_report = now + health_interval
            elif health_changed:
                health_suppressed += 1
        elif now >= next_health_report:
            # Quiet and healthy for a full interval: rearm, so a NEW problem is
            # reported at once instead of waiting out an old backoff.
            health_interval = max(args.poll_seconds, 60.0)

        last_err_state = bool(err)
        fail_message = err
        if not err:
            fail_streak = 0

        if err:
            if args.probe_fail_limit and fail_streak >= args.probe_fail_limit:
                say("PROBE-DEAD",
                    "the probe failed {} times in a row and never recovered; "
                    "this watcher has been BLIND, not calm. Last error: {}"
                    .format(fail_streak, err))
                return EXIT_PROBE_DEAD
        else:
            advanced = _advanced(value, mark, seen, args.probe_direction)
            if advanced:
                delta = "" if last_value is None else " ({:+g})".format(value - last_value)
                mark = value if mark is None else (
                    max(mark, value) if args.probe_direction == "up" else
                    min(mark, value) if args.probe_direction == "down" else value)
                if stalled:
                    say("RECOVER", "probe moved to {}{} after {} of silence"
                        .format(_fmt_num(value), delta, _fmt_secs(now - last_move)))
                    stalled = False
                    stall_since = None
                    last_progress_emit = now
                elif now - last_progress_emit >= args.progress_every:
                    say("PROGRESS", "probe={}{} elapsed={}".format(
                        _fmt_num(value), delta, _fmt_secs(now - started)))
                    last_progress_emit = now
                last_move = now
            elif value != last_value and now - last_progress_emit >= args.progress_every:
                # Movement, but not forward. Worth saying; NOT worth resetting
                # the stall timer for. A value flickering 41,42,41,42 is a retry
                # loop going nowhere, and used to read as continuous PROGRESS.
                say("DROP", "probe moved {} -> {} but that is not progress "
                            "(direction={}, mark={}); the stall timer keeps running"
                    .format(_fmt_num(last_value), _fmt_num(value),
                            args.probe_direction, _fmt_num(mark)))
                last_progress_emit = now
            if value is not None:
                seen[value] = None
                while len(seen) > 256:
                    seen.popitem(last=False)
            last_value = value

        if args.target is not None and last_value is not None \
                and _hit(last_value, args.target, args.probe_direction):
            if start_value is not None and last_value == start_value \
                    and not args.target_allow_preexisting:
                say("TARGET-PRESET",
                    "probe={} meets target {} but has not moved since START; "
                    "refusing to report success for work this watcher never saw"
                    .format(_fmt_num(last_value), _fmt_num(args.target)))
                return EXIT_TARGET_PRESET
            note = ""
            if args.pid is not None and not pid_alive(args.pid):
                note = (" NOTE: the watched pid {} is already GONE, so the "
                        "target was met by a process that has exited -- verify "
                        "the output before calling this a success.".format(args.pid))
            say("TARGET", "probe={} reached target {} after {}.{}".format(
                _fmt_num(last_value), _fmt_num(args.target),
                _fmt_secs(now - started), note))
            return EXIT_OK

        if not stalled and (now - last_move) >= args.stall_seconds:
            stalled = True
            stall_since = last_move
            stall_interval = max(args.stall_seconds, 60.0)
            next_stall_report = now + stall_interval
            say("STALL", "no progress for {} (probe stuck at {}); process={}; "
                         "elapsed={}".format(_fmt_secs(now - last_move),
                                             _fmt_num(last_value), alive_word(),
                                             _fmt_secs(now - started)))
        elif stalled and now >= next_stall_report:
            # A stall announced ONCE and then never again is how a dead job at
            # hour 1 of a 12-hour watch reads as calm. Backoff keeps the total
            # event count small enough not to trip Monitor's auto-stop.
            stall_interval = min(stall_interval * 2, 3600.0)
            next_stall_report = now + stall_interval
            say("STALL", "STILL stalled -- no progress for {} (probe stuck at "
                         "{}); process={}; elapsed={}".format(
                             _fmt_secs(now - stall_since), _fmt_num(last_value),
                             alive_word(), _fmt_secs(now - started)))

        if args.pid is not None and not pid_alive(args.pid):
            # Deliberately AFTER the probe read, so the final units of work are
            # counted before the watch ends.
            say("EXIT", "pid {} is gone; final probe={}; elapsed={}. Exit STATUS "
                        "is not observable for a non-child process -- check the "
                        "job's own log before calling this a success."
                .format(args.pid, _fmt_num(last_value), _fmt_secs(now - started)))
            return EXIT_OK

        if args.max_seconds and (now - started) >= args.max_seconds:
            say("TIMEOUT", "watch gave up after {}; probe={}".format(
                _fmt_secs(now - started), _fmt_num(last_value)))
            return EXIT_TIMEOUT

        if args.heartbeat_seconds and \
                time.monotonic() - _LAST_EMIT[0] >= args.heartbeat_seconds:
            # Never reassuring by accident: if the job is stalled, the heartbeat
            # says so rather than reporting "no news".
            news = ("STILL STALLED for {}".format(_fmt_secs(now - stall_since))
                    if stalled else "no news")
            say("ALIVE", "watching; probe={}; process={}; elapsed={}; {}".format(
                _fmt_num(last_value), alive_word(), _fmt_secs(now - started), news))


def _hit(value, target, direction) -> bool:
    return value <= target if direction == "down" else value >= target


def _advanced(value, mark, seen, direction) -> bool:
    """Is this value real forward progress, or just movement?"""
    if value is None:
        return False
    if mark is None:
        return True
    if direction == "up":
        return value > mark
    if direction == "down":
        return value < mark
    return value not in seen


# ------------------------------------------------------------------ entry point
def _install_signal_handlers():
    def handler(signum, _frame):
        raise _Stopped(signum)
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):        # not the main thread / unsupported
            pass


def run(argv=None) -> int:
    """main() wrapped so the watcher can never end without saying so."""
    _install_signal_handlers()
    argv_list = list(sys.argv[1:] if argv is None else argv)
    label = "-"
    for i, token in enumerate(argv_list):       # both --label X and --label=X
        if token == "--label" and i + 1 < len(argv_list):
            label = argv_list[i + 1]
        elif token.startswith("--label="):
            label = token.split("=", 1)[1]
    try:
        return main(argv)
    except _Stopped as stop:
        try:
            name = signal.Signals(stop.signum).name
        except Exception:
            name = "signal {}".format(stop.signum)
        emit("STOPPED", label,
             "this watcher received {} and is exiting. THE JOB IS NOT BEING "
             "WATCHED AFTER THIS LINE -- it was not stopped, and nothing further "
             "will be reported about it.".format(name))
        return EXIT_SIGNALLED
    except KeyboardInterrupt:
        emit("STOPPED", label, "this watcher was interrupted and is exiting. THE "
                               "JOB IS NOT BEING WATCHED AFTER THIS LINE.")
        return EXIT_SIGNALLED
    except BrokenPipeError:
        raise
    except SystemExit:
        raise
    except BaseException as exc:
        # A watcher that dies with a bare traceback, under a driver that reads
        # only stdout, is indistinguishable from a calm healthy job.
        try:
            emit("WATCHER-DIED", label,
                 "unexpected {} in the watcher: {}. THE JOB IS NO LONGER BEING "
                 "WATCHED.".format(type(exc).__name__, exc))
        except Exception:
            pass
        return EXIT_WATCHER_DIED


if __name__ == "__main__":
    try:
        sys.exit(run())
    except BrokenPipeError:
        # Our reader went away (piped into `head`, terminal closed). Nothing to
        # report to, and Python would otherwise print a second traceback at
        # shutdown while flushing stdout.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except Exception:
            pass
        os._exit(EXIT_OK)
