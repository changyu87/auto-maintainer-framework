#!/usr/bin/env python3
"""adapter_scaffold — the BYO-adapter authoring/scaffold tool (DESIGN §3.4.4).

The provider-supplied convenience that makes bring-your-own-adapter a CHECKED
operation. Given a port name, it:

  1. EMITS a skeleton script adapter — a self-contained Python module conforming
     to the adapter factory convention (DESIGN §3.4.3 / adapter-wiring): a module
     exposing ``factory(runtime) -> (StateManifest, run_callable)`` where
     ``run_callable`` has the fsm-contracts signature ``run(TickContext) ->
     StateResult``. The skeleton templates the full contract shape (manifest with
     reads/writes/emits, a deterministic ``run`` writing its product slot, and an
     explanatory ``# TODO`` for the author's logic). It is written to
     ``${project_dir}/.auto-maintainer/adapters/<port_lower>.py``.
  2. WIRES the port into the project-local config — the adapter-map entry
     (``port -> "<module>:factory"``) AND the route (inserting the new state
     between two existing states), reusing scheduling's route_config /
     adapter_map_config load-modify-save plumbing.
  3. RUNS a contract-conformance validator over the resulting wiring — reusing
     ``adapter_wiring.build_loop`` / ``validate_wiring``, which themselves reuse
     ``tick_orchestrator.validate_signals`` + ``validate_data_readiness`` and the
     ``fsm-contracts`` route schema + anchor invariants. If the emitted adapter
     does not resolve or the wiring does not validate, the scaffold REJECTS the
     operation (non-zero exit) and rolls back every file it wrote, so a failed
     scaffold leaves NO partial config behind.

This is sequenced AFTER (1) the route-as-data + adapter-wiring mechanism
(§3.4.3) and (2) the reference IMPLEMENT adapter (the act-side contract shape),
exactly as DESIGN §3.4.4 mandates ("SDK ergonomics follow once contracts are
battle-tested"): the emitted skeleton mirrors the shipped adapters'
manifest+factory shape, and the validator it runs is the SAME one the loop runs
at load.

It lives in scheduling (alongside route_config + adapter_map_config) because it
needs scheduling's DEFAULT_ROUTE + DEFAULT_ADAPTER_MAP defaults and reuses those
sibling CLIs' load-modify-save helpers. adapter-wiring + agent-dispatch +
fsm-contracts + tick-orchestrator are CONSUMED UNCHANGED (their validators); this
module never modifies them.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when scheduling moves to a different clock
  source (e.g. a native plugin cron API), or when a native rabbit/plugin config
  system subsumes the wiring-config CLIs.
"""

import argparse
import os
import sys

_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
_FEATURE_DIR = os.path.dirname(_SRC)
_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring",
             "prioritize", "implement", "safety-governance", "agent-dispatch",
             "observability", "verify-integrate"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if os.path.isdir(_dep_src) and _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import adapter_wiring as aw  # noqa: E402
import run_tick as rt  # noqa: E402
import route_config as rc  # noqa: E402
import adapter_map_config as amc  # noqa: E402


# The project-local dir adapters are emitted into. It is added to sys.path at
# validation time so the emitted "<module>:factory" address resolves by import.
_ADAPTERS_DIRNAME = "adapters"

# The factory entry-point name the skeleton exposes (the adapter-map address is
# "<module>:factory"). Fixed so the generated address is predictable.
_FACTORY_NAME = "factory"


class ScaffoldError(Exception):
    """Raised when a scaffold request is malformed or its resulting wiring does
    not validate. The message NAMES the offending port/file so the failure is
    locatable to a source artifact (spec-rules §1: determinism)."""


def _adapters_dir(project_dir):
    return os.path.join(project_dir, ".auto-maintainer", _ADAPTERS_DIRNAME)


def _module_name(port):
    """The emitted module's importable name: the port lowercased. The
    adapter-map address is "<module_name>:factory"."""
    return port.lower()


def _adapter_path(project_dir, port):
    return os.path.join(_adapters_dir(project_dir), _module_name(port) + ".py")


def _slot_name(port):
    """The product slot the skeleton writes — a fresh, port-derived name so the
    scaffolded state is data-ready (it reads nothing, writes its own slot)."""
    return _module_name(port) + "_result"


def render_skeleton(port):
    """Return the source text of a skeleton script adapter for `port`.

    The skeleton conforms to the adapter factory convention: a module-level
    ``MANIFEST`` (fsm-contracts StateManifest), a ``run(ctx) -> StateResult``
    callable, and ``factory(runtime) -> (MANIFEST, run)``. It reads NO slots and
    writes its own product slot, emitting OK / EMPTY — the minimal shape that is
    signal-valid AND data-ready wherever it is inserted, so the author can fill
    in real logic without first fighting the validator. Pure string render; no
    file I/O, fully deterministic."""
    module = _module_name(port)
    slot = _slot_name(port)
    return f'''#!/usr/bin/env python3
"""{module} — a scaffolded BYO adapter for the '{port}' port.

Generated by the auto-maintainer adapter-scaffold tool (DESIGN §3.4.4). This is a
SKELETON conforming to the adapter factory convention (DESIGN §3.4.3): a module
exposing ``factory(runtime) -> (StateManifest, run_callable)`` where
``run_callable`` has the fsm-contracts signature ``run(TickContext) ->
StateResult``. Fill in the TODO with your logic; keep the factory signature.

The maintainer loads this adapter by its map address "{module}:factory". Its
manifest declares the slots it reads/writes and the signals it emits — the
loop's load-time validator (adapter-wiring) checks the wiring against it.
"""

import fsm_contracts as fc


# This adapter's product slot. The skeleton reads NOTHING and writes this single
# fresh slot, so it is data-ready wherever it is inserted in the route. Add slots
# you actually consume to ``reads`` once your logic needs them (the validator will
# then require a predecessor to write them).
{slot.upper()}_SLOT = "{slot}"

# The state's bounded-scope contract: what it reads, writes, and the closed set
# of signals it may emit. Edit these to match your logic — but every signal you
# emit MUST be listed here, and every slot you read MUST be written by a
# predecessor on every path (the validator enforces both).
MANIFEST = fc.StateManifest(
    reads=[],
    writes=[{slot.upper()}_SLOT],
    emits=["OK", "EMPTY"],
)


def run(ctx):  # noqa: ARG001 - the skeleton reads no slots yet
    """The '{port}' state callable: ``run(TickContext) -> StateResult``.

    TODO: implement your adapter. Read the slots you need via ``ctx.read(name)``,
    do your work, and return a StateResult whose ``signal`` is one of
    MANIFEST.emits and whose ``writes`` populates the slots in MANIFEST.writes.

    The skeleton is a deterministic no-op: it writes an empty product and emits
    OK. Keep it pure (no wall-clock, randomness, or hidden I/O) so the loop stays
    replayable, OR move side effects behind the ``runtime`` config the factory
    binds.
    """
    return fc.StateResult(signal="OK", writes={{{slot.upper()}_SLOT: None}})


def {_FACTORY_NAME}(runtime):  # noqa: ARG001 - bind runtime config here if needed
    """The adapter-wiring factory: ``factory(runtime) -> (StateManifest,
    run_callable)``. ``runtime`` carries the resolved runtime dir
    (``runtime['project_dir']``) plus any injected config. Bind what your ``run``
    needs from it here (close over it), then return the manifest + run callable.
    """
    return MANIFEST, run
'''


def _validate_wiring(project_dir):
    """Resolve the ACTIVE route + adapter-map through adapter_wiring.build_loop —
    the SAME load-time validator the loop runs — with the project's adapters dir
    on sys.path so an emitted "<module>:factory" address imports. Raises
    ScaffoldError (wrapping any WiringError) on any failure; returns None on
    success."""
    adapters_dir = _adapters_dir(project_dir)
    added = False
    if os.path.isdir(adapters_dir) and adapters_dir not in sys.path:
        sys.path.insert(0, adapters_dir)
        added = True
    try:
        route = rc.load_route(project_dir)
        amap = amc.load_map(project_dir)
        runtime = {
            "project_dir": project_dir,
            "runtime_dir": os.path.join(project_dir, ".auto-maintainer"),
            "source": None,
            "now": None,
            "governance": {"mode": "dry-run"},
        }
        states = aw.resolve_states(route, amap, runtime)
        manifests = {name: m for name, (m, _r) in states.items()}
        verdict = aw.validate_wiring(
            route, manifests, "GUARD", rt._INITIAL_SLOTS)
        if not verdict.passed:
            raise ScaffoldError(
                "scaffolded wiring is invalid: " + "; ".join(verdict.messages))
    except aw.WiringError as exc:
        raise ScaffoldError(f"scaffolded wiring is invalid: {exc}")
    finally:
        if added and adapters_dir in sys.path:
            sys.path.remove(adapters_dir)


def scaffold(project_dir, port, after, before):
    """Emit + wire + VALIDATE a new BYO script adapter for `port`.

    Writes the skeleton to ${{project_dir}}/.auto-maintainer/adapters/<port>.py,
    sets the adapter-map entry ``port -> "<module>:factory"``, inserts the state
    into the route between `after` and `before`, then validates the whole wiring
    via adapter_wiring.build_loop. On ANY failure it rolls back every file it
    created/changed (so a failed scaffold leaves no partial config) and raises
    ScaffoldError. Returns the emitted adapter path on success.

    `port` must be a fresh state (not already in the route) and `after`/`before`
    must be existing route states — the scaffold tool authors NEW ports, not
    overrides of existing ones (use adapter-map set-script for that)."""
    if not port or not port.strip():
        raise ScaffoldError("port name must be a non-empty string")
    port = port.strip()

    route = rc.load_route(project_dir)
    if port in route["states"]:
        raise ScaffoldError(
            f"port '{port}' is already a route state; the scaffold tool authors "
            f"NEW ports (use the adapter-map editor to swap an existing one)")
    if after not in route["states"]:
        raise ScaffoldError(f"--after state '{after}' is not in the route")
    if before not in route["states"]:
        raise ScaffoldError(f"--before state '{before}' is not in the route")

    adapter_path = _adapter_path(project_dir, port)
    if os.path.exists(adapter_path):
        raise ScaffoldError(
            f"adapter file already exists at {adapter_path}; refusing to "
            f"overwrite")

    # Snapshot the prior on-disk config so a failed validation rolls back cleanly.
    route_path = rc._override_path(project_dir)
    map_path = amc._override_path(project_dir)
    prior_route = _read_or_none(route_path)
    prior_map = _read_or_none(map_path)

    address = f"{_module_name(port)}:{_FACTORY_NAME}"
    wrote_adapter = False
    try:
        # 1. Emit the skeleton adapter file.
        os.makedirs(_adapters_dir(project_dir), exist_ok=True)
        with open(adapter_path, "w") as f:
            f.write(render_skeleton(port))
        wrote_adapter = True

        # 2. Wire the adapter-map + route via the sibling CLIs' save helpers
        #    (load-modify-save), WITHOUT their per-edit validate (we validate the
        #    combined result once below, after both edits are in place).
        amap = amc.load_map(project_dir)
        amap[port] = address
        amc._save_map(amap, project_dir)

        edited = rc._insert_state(rc.load_route(project_dir), port, after, before)
        rc._save_route(edited, project_dir)

        # 3. Contract-conformance: resolve + validate the WHOLE wiring (reusing
        #    tick-orchestrator's validators + fsm-contracts via build_loop).
        _validate_wiring(project_dir)
    except (ScaffoldError, aw.WiringError, ValueError, OSError) as exc:
        # Roll back every file we touched so a failed scaffold leaves no mess.
        _restore(route_path, prior_route)
        _restore(map_path, prior_map)
        if wrote_adapter and os.path.exists(adapter_path):
            os.remove(adapter_path)
        if isinstance(exc, ScaffoldError):
            raise
        raise ScaffoldError(str(exc))

    return adapter_path


def _read_or_none(path):
    if os.path.isfile(path):
        with open(path) as f:
            return f.read()
    return None


def _restore(path, prior):
    """Restore `path` to its prior content (or remove it if there was none)."""
    if prior is None:
        if os.path.isfile(path):
            os.remove(path)
    else:
        with open(path, "w") as f:
            f.write(prior)


def main(argv=None):
    """The scaffold CLI entrypoint. Returns the process exit code.

    Subcommands:
      --emit --port P            print the skeleton adapter to stdout (no write).
      new --port P --after A --before B
                                 emit + wire (map + route) + VALIDATE the new
                                 adapter; reject (non-zero, full rollback) on any
                                 validation failure.
    """
    parser = argparse.ArgumentParser(
        description="Scaffold a bring-your-own adapter (emit + wire + VALIDATE).")
    parser.add_argument("--project-dir", dest="project_dir")
    parser.add_argument("--emit", action="store_true",
                        help="print the skeleton adapter for --port (no write)")
    parser.add_argument("--port", dest="emit_port",
                        help="port to emit the skeleton for (with --emit)")
    sub = parser.add_subparsers(dest="cmd")

    p_new = sub.add_parser(
        "new", help="emit + wire + validate a new BYO adapter")
    p_new.add_argument("--project-dir", dest="project_dir")
    p_new.add_argument("--port", required=True)
    p_new.add_argument("--after", required=True,
                       help="existing route state the new state follows")
    p_new.add_argument("--before", required=True,
                       help="existing route state the new state precedes")

    args = parser.parse_args(argv)
    project_dir = args.project_dir or os.getcwd()

    if args.emit:
        if not args.emit_port:
            sys.stdout.write("REJECTED: --emit requires --port\n")
            return 1
        sys.stdout.write(render_skeleton(args.emit_port))
        return 0

    if args.cmd == "new":
        try:
            path = scaffold(project_dir, args.port, args.after, args.before)
        except ScaffoldError as exc:
            sys.stdout.write(f"REJECTED: {exc}\n")
            return 1
        sys.stdout.write(
            f"OK: scaffolded adapter for '{args.port}' -> {path}\n"
            f"    wired adapter-map + route (validated). Edit the TODO in "
            f"{path} to implement your logic.\n")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
