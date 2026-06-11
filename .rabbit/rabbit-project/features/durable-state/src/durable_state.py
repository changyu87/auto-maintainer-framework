#!/usr/bin/env python3
"""durable-state — the resumability backbone for the tick FSM.

This module provides the crash-safety primitives a tick needs to be finished
(not redone) after a truncated run:

  1. DurableState  — load/save a single semver'd JSON document
                     ({schema_version, ...slots}). The on-disk file is the SOLE
                     source of truth (DESIGN §3.1.4). Writes are atomic
                     (temp + rename) so a crash never leaves a torn file.
  2. Journal       — append-only per-tick journal with record-before-act: the
                     intent of an outward/mutating effect is journaled BEFORE
                     the effect runs, each intent carrying a stable dedup_key.
  3. DRAIN state   — run(TickContext) -> StateResult. Entry step that finishes
                     owed work from a prior truncated tick before any new work:
                     it re-applies recorded-but-not-confirmed intents
                     idempotently (a target value is re-applied, never
                     re-incremented). Emits OK.
  4. PERSIST state — run(TickContext) -> StateResult. Writes the durable state
                     from TickContext slots to disk. Emits OK.

Idempotency / dedup convention: every owed intent carries a stable dedup_key
and a TARGET value. Replaying it sets the durable counter to the target, so a
re-run or DRAIN replay never double-acts (DESIGN §3.2.4).

durable-state CONSUMES fsm-contracts (TickContext, StateResult, StateManifest);
it never edits or forks that module. The state + journal file paths are
injected via TickContext slots ("state_path", "journal_path") so tests use a
temp path and the on-disk file is the only source of truth.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: superseded when the durable-state schema reaches a
  breaking major version (e.g. v2 adds compaction/rotation, DESIGN §3.2.5) or
  when the persistence layer is replaced.
"""

import json
import os
import tempfile

import fsm_contracts as fc

# The durable state document's schema version. A breaking change to the slot
# shape bumps this (and is the deprecation trigger named above).
SCHEMA_VERSION = "1.0.0"

# The default durable document when no file exists yet.
_DEFAULT_DOC = {"schema_version": SCHEMA_VERSION, "counter": 0}


def _atomic_write_json(path, doc):
    """Write `doc` as JSON to `path` atomically (temp file + rename).

    The temp file is created in the same directory so os.replace is a true
    atomic rename on POSIX filesystems; a crash mid-write leaves either the old
    file or the new one, never a torn file.
    """
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".durable.", dir=parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# 1. DurableState — versioned JSON document; on-disk file is the only truth.
# --------------------------------------------------------------------------

class DurableState:
    """A single semver'd JSON document persisted at an injected path.

    The on-disk file is the sole source of truth: every load() reads committed
    bytes, never in-process memory. save() is atomic (temp + rename).
    """

    def __init__(self, path):
        self.path = path

    def load(self):
        """Return the durable document, or the default when no file exists."""
        if not os.path.isfile(self.path):
            return dict(_DEFAULT_DOC)
        with open(self.path) as fh:
            return json.load(fh)

    def save(self, doc):
        """Atomically persist `doc` (a {schema_version, ...slots} mapping)."""
        _atomic_write_json(self.path, doc)


# --------------------------------------------------------------------------
# 2. Journal — append-only, record-before-act, stable dedup_key.
# --------------------------------------------------------------------------

class Journal:
    """Append-only per-tick journal of outward-effect intents (JSONL).

    record-before-act: record(intent) durably appends the intent BEFORE the
    effect runs. Each intent carries a stable dedup_key. confirm(dedup_key)
    appends a confirmation marker, so unconfirmed() (recorded-but-not-confirmed
    intents) drives skip-on-resume.
    """

    def __init__(self, path):
        self.path = path

    def _read_lines(self):
        if not os.path.isfile(self.path):
            return []
        with open(self.path) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _append(self, record):
        parent = os.path.dirname(self.path) or "."
        os.makedirs(parent, exist_ok=True)
        with open(self.path, "a") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def record(self, intent):
        """Durably append an intent (must carry a 'dedup_key') before acting."""
        if "dedup_key" not in intent:
            raise ValueError("journal intent must carry a 'dedup_key'")
        self._append({"kind": "intent", **intent})

    def confirm(self, dedup_key):
        """Append a confirmation marker for an already-recorded intent."""
        self._append({"kind": "confirm", "dedup_key": dedup_key})

    def entries(self):
        """Return the recorded intents, in append order."""
        return [r for r in self._read_lines() if r.get("kind") == "intent"]

    def confirmed_keys(self):
        """Return the set of dedup_keys that have a confirmation marker."""
        return {r["dedup_key"] for r in self._read_lines()
                if r.get("kind") == "confirm"}

    def unconfirmed(self):
        """Return dedup_keys recorded-but-not-confirmed, in append order."""
        confirmed = self.confirmed_keys()
        return [e["dedup_key"] for e in self.entries()
                if e["dedup_key"] not in confirmed]


# --------------------------------------------------------------------------
# 3+4. DRAIN / PERSIST — fsm-contracts state contract: run(ctx) -> StateResult.
# --------------------------------------------------------------------------

# Per-state manifests (fsm-contracts {reads, writes, emits}). Both states read
# the injected file paths; DRAIN reconciles the counter from owed journal work,
# PERSIST writes the counter through to disk. Both emit the closed signal OK.
DRAIN_MANIFEST = fc.StateManifest(
    reads=["state_path", "journal_path"],
    writes=["counter"],
    emits=["OK"],
)

PERSIST_MANIFEST = fc.StateManifest(
    reads=["state_path", "counter"],
    writes=[],
    emits=["OK"],
)


def drain_run(ctx):
    """DRAIN: finish owed work from a prior truncated tick, idempotently.

    Scans the journal for intents recorded-but-not-confirmed and reconciles
    each by RE-APPLYING its target value to the durable counter (never
    re-incrementing), then confirms it. Brings durable state to the intended
    value EXACTLY ONCE. Emits OK (whether or not anything was owed).
    """
    state_path = ctx.read("state_path")
    journal_path = ctx.read("journal_path")
    state = DurableState(state_path)
    journal = Journal(journal_path)

    doc = state.load()
    owed = journal.unconfirmed()
    if owed:
        entries = {e["dedup_key"]: e for e in journal.entries()}
        for key in owed:
            target = entries[key]["target_counter"]
            doc["counter"] = target  # idempotent: set target, never increment
            state.save(doc)
            journal.confirm(key)

    return fc.StateResult(signal="OK", writes={"counter": doc["counter"]})


def persist_run(ctx):
    """PERSIST: write the durable state from TickContext slots to disk.

    The resumability backbone: the counter slot is flushed through to the
    durable document atomically. Emits OK.
    """
    state_path = ctx.read("state_path")
    counter = ctx.read("counter")
    state = DurableState(state_path)
    doc = state.load()
    doc["counter"] = counter
    state.save(doc)
    return fc.StateResult(signal="OK")
