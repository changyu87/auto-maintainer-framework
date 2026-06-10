#!/usr/bin/env python3
"""Test runner for fsm-contracts.

Discovers every test_*.py module in this directory, runs each test_* function,
and exits nonzero if any fails. No third-party deps; no interactive constructs.

Owner: changyu87
"""

import importlib.util
import os
import sys
import traceback

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_module(path):
    name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    test_files = sorted(
        os.path.join(_TEST_DIR, f)
        for f in os.listdir(_TEST_DIR)
        if f.startswith("test_") and f.endswith(".py")
    )
    if not test_files:
        sys.stderr.write("no test_*.py files found\n")
        return 1

    passed = 0
    failed = []
    for path in test_files:
        module = _load_module(path)
        fns = sorted(
            n for n in dir(module)
            if n.startswith("test_") and callable(getattr(module, n))
        )
        for fn_name in fns:
            fn = getattr(module, fn_name)
            try:
                fn()
                passed += 1
                sys.stdout.write(f"PASS {module.__name__}::{fn_name}\n")
            except Exception:
                failed.append(f"{module.__name__}::{fn_name}")
                sys.stdout.write(f"FAIL {module.__name__}::{fn_name}\n")
                traceback.print_exc()

    sys.stdout.write(f"\n{passed} passed, {len(failed)} failed\n")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
