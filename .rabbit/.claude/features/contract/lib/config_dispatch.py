"""contract.lib.config_dispatch — reusable CORE of the per-feature config interpreter.

A per-feature config command's interpreter is a generic dispatcher that
enumerates every feature's CONFIGURATION declarations, validates the user
value, applies the mutation via contract.lib.mutation, and emits a branded
"restart Claude" prompt when the configurable is restart-gated. This module
holds that generic flow so each per-feature config command (e.g.
`/rabbit-cage-config`, `/rabbit-tdd-autonomous`) becomes a THIN wrapper over
it — the interpreter logic lives ONCE in contract.lib (script > prompt; no N
drifting copies).

Public API:
    dispatch_config(cfg, value, *, repo_root, feature_dir=None,
                    template_value=None) -> dict

`cfg` is a single `configuration[]` entry (see configuration.schema.json):
it declares `subcommand`, exactly one of `values`/`actions` (each maps a key
to an `{api, args}` call), an optional `validation` block, and an optional
`restart_required` flag. The helper validates, dispatches the mutation through
contract.lib.mutation (it NEVER re-implements a primitive), emits the restart
prompt when warranted, and returns a machine-first structured dict. It NEVER
prints and NEVER calls sys.exit — the thin per-feature command owns IO.

Result dict shape:
    {"ok": bool, "messages": list[str], "restart_prompt": str | None,
     "error": str | None}

Version: 1.0.0
Owner: rabbit-workflow team (contract)
Deprecation criterion: when the rabbit CLI exposes native per-feature
    configuration mutation.
"""

import os
import re
import sys

from lib import mutation

_PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")


def _result(ok, *, messages=None, restart_prompt=None, error=None):
    return {
        "ok": ok,
        "messages": list(messages or []),
        "restart_prompt": restart_prompt,
        "error": error,
    }


def _has_templates(obj):
    """Return True if any string value in obj contains a {placeholder}."""
    if isinstance(obj, dict):
        return any(_has_templates(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_templates(item) for item in obj)
    if isinstance(obj, str):
        return bool(_PLACEHOLDER_RE.search(obj))
    return False


def _apply_template(obj, value):
    """Replace {tool}, {command}, {value} placeholders with value; returns new obj."""
    if isinstance(obj, dict):
        return {k: _apply_template(v, value) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_apply_template(item, value) for item in obj]
    if isinstance(obj, str):
        return (obj.replace("{tool}", value)
                   .replace("{command}", value)
                   .replace("{value}", value))
    return obj


def _validate(validation, user_value, subcommand):
    """Apply validation rules to user_value. Return an error string or None."""
    if not validation:
        return None
    reject_prefix = validation.get("reject_prefix")
    if reject_prefix and user_value.startswith(reject_prefix):
        return (f"{subcommand}: value must not start with '{reject_prefix}' "
                "(use bash-allow for Bash commands)")
    reject_chars = validation.get("reject_chars")
    if reject_chars and re.search(f"[{reject_chars}]", user_value):
        return (f"{subcommand}: value contains a forbidden character "
                f"(reject pattern: {reject_chars!r})")
    return None


def _restart_prompt(subcommand):
    """Render the branded restart-Claude line via the rabbit_print convention.

    rabbit_print lives under contract/scripts/; make it importable the same way
    a per-feature config interpreter does, then delegate the framing to it.
    """
    scripts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from rabbit_print import rabbit_subline  # noqa: PLC0415
    return rabbit_subline(
        f"restart Claude (exit + relaunch) for the new {subcommand} "
        "value to take effect",
        color="red",
        icon="\U0001f504",
    )


def dispatch_config(cfg, value, *, repo_root, feature_dir=None, template_value=None):
    """Run the generic config-mutation flow for one configuration[] entry.

    cfg            — a single configuration[] entry (validate + mutate target).
    value          — values-style: the value key; actions-style: the action key.
    repo_root      — repo root for filesystem-targeted mutation primitives.
    feature_dir    — feature dir for the run_feature_script escape hatch.
    template_value — for an actions-style entry whose args carry {placeholder}s,
                     the user-supplied string substituted into them.

    Returns the structured result dict (see module docstring). NEVER prints,
    NEVER sys.exit; ALWAYS delegates the mutation to contract.lib.mutation.
    """
    subcommand = cfg.get("subcommand", cfg.get("id", "config"))
    validation = cfg.get("validation")
    values = cfg.get("values")
    actions = cfg.get("actions")

    if values is not None:
        err = _validate(validation, value, subcommand)
        if err:
            return _result(False, error=err)
        if value not in values:
            valid = ", ".join(sorted(values.keys()))
            return _result(False, error=(
                f"{subcommand}: unknown value '{value}' (valid values: {valid})"))
        api_call = values[value]
        api_args = dict(api_call.get("args") or {})

    elif actions is not None:
        if value not in actions:
            valid = ", ".join(sorted(actions.keys()))
            return _result(False, error=(
                f"{subcommand}: unknown action '{value}' (valid actions: {valid})"))
        api_call = actions[value]
        api_args = dict(api_call.get("args") or {})
        if _has_templates(api_args):
            if template_value is None:
                return _result(False, error=(
                    f"{subcommand} {value}: requires a value argument"))
            err = _validate(validation, template_value, subcommand)
            if err:
                return _result(False, error=err)
            api_args = _apply_template(api_args, template_value)

    else:
        return _result(False, error=(
            f"{subcommand}: this configurable declares neither values nor actions"))

    api_name = api_call["api"]
    fn = getattr(mutation, api_name, None)
    if fn is None:
        return _result(False, error=f"{subcommand}: unknown mutation API '{api_name}'")

    if api_name == "run_feature_script":
        check = fn(**api_args, feature_dir=feature_dir)
    else:
        check = fn(**api_args, repo_root=repo_root)

    messages = list(getattr(check, "messages", None) or [])
    if not getattr(check, "passed", False):
        return _result(False, messages=messages,
                       error=f"{subcommand}: mutation failed")

    restart_prompt = None
    if cfg.get("restart_required"):
        changed_state = not any("no-op" in m for m in messages)
        if changed_state:
            restart_prompt = _restart_prompt(subcommand)

    return _result(True, messages=messages, restart_prompt=restart_prompt)
