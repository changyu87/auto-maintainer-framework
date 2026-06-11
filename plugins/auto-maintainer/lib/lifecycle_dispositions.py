#!/usr/bin/env python3
"""lifecycle-dispositions — the loop's coarse cross-tick operating condition.

This module owns the loop's **disposition** (a closed, durable operating
condition) and the two anchor states that read/select it:

  - GUARD — the entry gate plus a single-writer mutex with stale-marker
    detection.
  - EXIT  — the terminal anchor that maps the tick outcome to the next
    disposition and emits the selecting signal, then releases the mutex.

Both GUARD and EXIT implement the fsm-contracts `run(TickContext) ->
StateResult` contract with per-state manifests, so tick-orchestrator can run
them as the route's entry and terminal anchors. This feature manages the
disposition + lock markers ONLY; it does NOT import durable-state.

Host-agnostic resumption: the on-disk disposition + lock markers are the sole
source of truth. Behaviour is identical on a fresh headless context or a warm
in-session one because every cross-tick fact is read back from disk.

The marker location is injectable: every public entry point takes a
``runtime_dir`` so tests pass a temp path and production passes the project
runtime dir.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when the lifecycle disposition model changes
  incompatibly (e.g. v2 parallelism introduces per-stream dispositions) or the
  marker encoding is replaced.
"""

import json
import os

# The fsm-contracts module is consumed via sys.path (the test harness and
# tick-orchestrator both insert ../fsm-contracts/src). Import it the same way.
import fsm_contracts as fc


class LifecycleError(Exception):
    """Raised on an invalid disposition value or a malformed marker."""


# --------------------------------------------------------------------------
# 1. Disposition — the closed operating-condition set + durable marker.
# --------------------------------------------------------------------------

class Disposition:
    """The closed set of loop operating conditions (spec §1.2).

    RUNNING        — actively ticking.
    IDLE           — queue empty; auto-resumes on the next heartbeat.
    STOPPED        — human stop; LATCHES until a human acts.
    ABORTED        — fault; LATCHES until a human acts.
    RESTART_NEEDED — restart owed; auto-resumes after a restart.
    """

    RUNNING = "RUNNING"
    IDLE = "IDLE"
    STOPPED = "STOPPED"
    ABORTED = "ABORTED"
    RESTART_NEEDED = "RESTART_NEEDED"

    _MEMBERS = (RUNNING, IDLE, STOPPED, ABORTED, RESTART_NEEDED)

    @classmethod
    def members(cls):
        return cls._MEMBERS

    @classmethod
    def is_member(cls, value):
        return value in cls._MEMBERS


# When no disposition marker exists yet the loop is considered IDLE: it has no
# work latched and resumes on the next heartbeat (spec §1.2).
_DEFAULT_DISPOSITION = Disposition.IDLE

_DISPOSITION_MARKER = "disposition"
_LOCK_MARKER = "lock.json"


def _disposition_path(runtime_dir):
    return os.path.join(runtime_dir, _DISPOSITION_MARKER)


def _lock_path(runtime_dir):
    return os.path.join(runtime_dir, _LOCK_MARKER)


def read_disposition(runtime_dir):
    """Read the durable disposition marker; default IDLE when unset."""
    path = _disposition_path(runtime_dir)
    if not os.path.isfile(path):
        return _DEFAULT_DISPOSITION
    with open(path, "r") as f:
        value = f.read().strip()
    if not Disposition.is_member(value):
        raise LifecycleError(
            f"disposition marker holds non-member value '{value}'")
    return value


def write_disposition(runtime_dir, value):
    """Write the durable disposition marker (closed-set validated)."""
    if not Disposition.is_member(value):
        raise LifecycleError(
            f"'{value}' is not a member of the Disposition set "
            f"{Disposition.members()}")
    os.makedirs(runtime_dir, exist_ok=True)
    path = _disposition_path(runtime_dir)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(value)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# 2. Single-writer mutex — lock marker with stale-marker detection.
# --------------------------------------------------------------------------
#
# Ownership identity is stamped as (PID, process start-time). The start-time
# guards against PID reuse: a recycled PID belonging to an unrelated process
# will not match the recorded start-time, so the lock is correctly judged
# stale. A lock is reclaimable when its holder is no longer the live owner.

def _proc_start_time(pid):
    """The process start-time field from /proc/<pid>/stat (clock ticks).

    Returns None when the PID is not alive or its start-time is unreadable.
    The start-time pins a lock to a specific process instance, defeating PID
    reuse.
    """
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            stat = f.read()
    except (OSError, IOError):
        return None
    # field 22 (1-indexed) is starttime; the comm field (2) may contain spaces
    # and is wrapped in parentheses, so split on the last ')'.
    try:
        after = stat.rsplit(")", 1)[1].split()
        # after[0] is state (field 3); starttime is field 22 -> after index 19.
        return after[19]
    except (IndexError, ValueError):
        return None


def _self_owner():
    pid = os.getpid()
    return {"pid": pid, "start_time": _proc_start_time(pid)}


def _read_lock(runtime_dir):
    """Return the lock record dict, or None when no lock marker exists."""
    path = _lock_path(runtime_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, IOError, ValueError):
        # A corrupt lock marker is treated as no live owner (reclaimable).
        return None


def _owner_is_live(record):
    """True when the lock record names a still-running owner process.

    A record is live when its PID is alive AND the recorded start-time matches
    the live process's start-time (defeating PID reuse). Any mismatch — dead
    PID, recycled PID, missing start-time — makes the lock STALE/reclaimable.
    """
    if not isinstance(record, dict):
        return False
    pid = record.get("pid")
    if not isinstance(pid, int):
        return False
    current = _proc_start_time(pid)
    if current is None:
        return False
    return current == record.get("start_time")


def acquire_lock(runtime_dir):
    """Acquire the single-writer mutex.

    Returns True when acquired (no live holder, or the existing holder's lock
    was stale and reclaimed), False when a LIVE holder already owns it.
    """
    os.makedirs(runtime_dir, exist_ok=True)
    record = _read_lock(runtime_dir)
    if record is not None and _owner_is_live(record):
        # A live holder owns the lock. If it is THIS process, the acquire is
        # idempotently refused (single-writer: exactly one acquire holds it).
        return False
    # No live holder (no marker, dead holder, or stale PID-reuse): claim it.
    path = _lock_path(runtime_dir)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_self_owner(), f)
    os.replace(tmp, path)
    return True


def lock_is_held(runtime_dir):
    """True when a live process currently holds the mutex."""
    return _owner_is_live(_read_lock(runtime_dir))


def release_lock(runtime_dir):
    """Release the mutex by removing the lock marker (idempotent)."""
    path = _lock_path(runtime_dir)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# --- test seams ---------------------------------------------------------

def dead_pid():
    """A PID that is guaranteed not to be a live process.

    A negative PID can never name a running process, so /proc/<pid>/stat is
    unreadable and the lock is unconditionally judged stale.
    """
    return -1


def write_stale_lock_for_test(runtime_dir, pid):
    """Stamp a lock marker owned by a (dead) PID, for stale-reclaim tests."""
    os.makedirs(runtime_dir, exist_ok=True)
    path = _lock_path(runtime_dir)
    with open(path, "w") as f:
        json.dump({"pid": pid, "start_time": "0"}, f)


# --------------------------------------------------------------------------
# 3. GUARD state — entry gate + single-writer mutex (fsm-contracts contract).
# --------------------------------------------------------------------------

# GUARD's signal vocabulary (per-state emits). Latched/owed dispositions short
# circuit the tick; a clean start admits with OK.
_GUARD_EMITS = ("OK", "HALT_REQUESTED", "RESTART_REQUIRED")


class Guard:
    """The route's entry anchor.

    Reads the on-disk disposition and decides whether the tick may proceed:
      - STOPPED / ABORTED  -> HALT_REQUESTED (latched; tick does NOT proceed)
      - RESTART_NEEDED     -> RESTART_REQUIRED (auto-resumes after a restart)
      - otherwise          -> acquire the single-writer mutex (reclaiming a
                              stale lock), set disposition RUNNING, emit OK.

    The runtime marker dir is injected at construction so the same Guard logic
    serves tests (temp dir) and production (project runtime dir).
    """

    def __init__(self, runtime_dir):
        self.runtime_dir = runtime_dir
        self.manifest = fc.StateManifest(reads=[], writes=[],
                                         emits=list(_GUARD_EMITS))

    def run(self, ctx):  # noqa: ARG002 - ctx is the contract arg; GUARD reads disk
        disposition = read_disposition(self.runtime_dir)
        if disposition in (Disposition.STOPPED, Disposition.ABORTED):
            return fc.StateResult(
                signal="HALT_REQUESTED",
                journal=[f"GUARD: disposition {disposition} is latched; halt"])
        if disposition == Disposition.RESTART_NEEDED:
            return fc.StateResult(
                signal="RESTART_REQUIRED",
                journal=["GUARD: RESTART_NEEDED; restart required"])
        # Clean start (RUNNING or IDLE): acquire the mutex, reclaiming a stale
        # lock. A live holder means a second instance is running — refuse.
        if not acquire_lock(self.runtime_dir):
            return fc.StateResult(
                signal="HALT_REQUESTED",
                journal=["GUARD: a live instance holds the mutex; halt"])
        write_disposition(self.runtime_dir, Disposition.RUNNING)
        return fc.StateResult(
            signal="OK",
            journal=["GUARD: mutex acquired; disposition RUNNING"])


# --------------------------------------------------------------------------
# 4. EXIT state — terminal anchor: tick outcome -> disposition + signal.
# --------------------------------------------------------------------------

# EXIT reads the tick outcome slot and selects the next disposition + signal.
_EXIT_OUTCOME_SLOT = "tick_outcome"
_EXIT_EMITS = ("refire", "idle", "break", "halt")

# Map a tick outcome to (next disposition, emitted signal).
_OUTCOME_TABLE = {
    "work-remains": (Disposition.RUNNING, "refire"),
    "empty": (Disposition.IDLE, "idle"),
    "restart": (Disposition.RESTART_NEEDED, "break"),
    "fault": (Disposition.ABORTED, "halt"),
}


class Exit:
    """The route's terminal anchor.

    Reads the tick outcome slot, selects the next disposition and emits the
    matching signal:
      work-remains -> RUNNING        / refire
      empty        -> IDLE           / idle   (rely on the heartbeat)
      restart      -> RESTART_NEEDED / break  (restart owed)
      fault        -> ABORTED        / halt   (latched fault)
    then releases the single-writer mutex.
    """

    def __init__(self, runtime_dir):
        self.runtime_dir = runtime_dir
        self.manifest = fc.StateManifest(reads=[_EXIT_OUTCOME_SLOT], writes=[],
                                         emits=list(_EXIT_EMITS))

    def run(self, ctx):
        outcome = ctx.read(_EXIT_OUTCOME_SLOT)
        if outcome not in _OUTCOME_TABLE:
            raise LifecycleError(
                f"EXIT: unknown tick outcome '{outcome}' "
                f"(expected one of {tuple(_OUTCOME_TABLE)})")
        next_disposition, signal = _OUTCOME_TABLE[outcome]
        write_disposition(self.runtime_dir, next_disposition)
        release_lock(self.runtime_dir)
        return fc.StateResult(
            signal=signal,
            journal=[f"EXIT: outcome {outcome} -> {next_disposition}/{signal}; "
                     "mutex released"])
