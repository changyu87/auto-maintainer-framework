#!/usr/bin/env python3
"""Rabbit test runner entry point for the phase-ports feature.

Discovers and runs every test_*.py module in this directory via unittest,
and exits non-zero on any failure so the rabbit TDD harness can detect red.

Version: 1.0.0
Owner: rabbit-workflow team
Deprecation criterion: superseded when per-port independent contract
  versioning replaces the single-bundle version (deferred to v2).
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# Make the feature dir importable so `import scripts.resolve_ports` resolves
# to the sibling library, independent of the harness cwd.
FEATURE_DIR = os.path.dirname(HERE)
if FEATURE_DIR not in sys.path:
    sys.path.insert(0, FEATURE_DIR)


def main():
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=HERE, pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
