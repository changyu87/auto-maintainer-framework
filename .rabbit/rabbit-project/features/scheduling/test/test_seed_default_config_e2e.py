"""seed_default_config (#211): a fresh install gets the aggressive default config
seeded into its runtime dir — idempotently, never clobbering an existing config."""
import json
import os
import sys
import tempfile

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
import start as sa  # noqa: E402


def _make_source(tmp):
    src = os.path.join(tmp, "default-config")
    os.makedirs(src)
    payload = {
        "config.json": '{"mode": "auto-merge"}',
        "route.json": '{"states": ["GUARD", "REVIEW"]}',
        "adapter-map.json": '{"GUARD": "run_tick:make_guard"}',
    }
    for name, body in payload.items():
        with open(os.path.join(src, name), "w") as fh:
            fh.write(body)
    return src


def test_seed_copies_all_when_absent():
    with tempfile.TemporaryDirectory() as t:
        src = _make_source(t)
        rt_dir = os.path.join(t, "runtime")
        seeded = sa.seed_default_config(rt_dir, source_dir=src)
        assert sorted(seeded) == ["adapter-map.json", "config.json", "route.json"]
        with open(os.path.join(rt_dir, "config.json")) as fh:
            assert json.load(fh)["mode"] == "auto-merge"


def test_seed_idempotent_and_never_clobbers_existing():
    with tempfile.TemporaryDirectory() as t:
        src = _make_source(t)
        rt_dir = os.path.join(t, "runtime")
        os.makedirs(rt_dir)
        # a config the user already wrote — must be preserved
        with open(os.path.join(rt_dir, "config.json"), "w") as fh:
            fh.write('{"mode": "propose"}')
        seeded = sa.seed_default_config(rt_dir, source_dir=src)
        assert "config.json" not in seeded  # not clobbered
        assert sorted(seeded) == ["adapter-map.json", "route.json"]
        with open(os.path.join(rt_dir, "config.json")) as fh:
            assert json.load(fh)["mode"] == "propose"  # user's value preserved
        # a second call seeds nothing (all present now) — idempotent
        assert sa.seed_default_config(rt_dir, source_dir=src) == []


def test_seed_noop_when_source_absent():
    with tempfile.TemporaryDirectory() as t:
        rt_dir = os.path.join(t, "runtime")
        assert sa.seed_default_config(rt_dir, source_dir=os.path.join(t, "nope")) == []
        assert not os.path.isdir(rt_dir) or os.listdir(rt_dir) == []
