"""migrate_seeded_config (#337): config is resolved at runtime
(override-else-default), so start no longer seeds. Instead it retires leftover
seed files — a runtime file byte-identical to the shipped default is a stale seed
(removed so the shipped default flows through on every release); a diverged file
is a genuine user override (kept untouched)."""
import os
import sys
import tempfile

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
import start as sa  # noqa: E402


def _make_source(tmp):
    """A stand-in shipped config/default dir with byte-stable defaults."""
    src = os.path.join(tmp, "config", "default")
    os.makedirs(src)
    payload = {
        "config.json": '{"mode": "auto-merge"}\n',
        "route.json": '{"states": ["GUARD", "REVIEW"]}\n',
        "adapter-map.json": '{"GUARD": "run_tick:make_guard"}\n',
    }
    for name, body in payload.items():
        with open(os.path.join(src, name), "w") as fh:
            fh.write(body)
    return src, payload


def _seed(rt_dir, src, names):
    """Copy the shipped defaults verbatim into the runtime dir (mimics an install
    seeded by the OLD copy-once path)."""
    os.makedirs(rt_dir, exist_ok=True)
    for name in names:
        with open(os.path.join(src, name), "rb") as fs:
            data = fs.read()
        with open(os.path.join(rt_dir, name), "wb") as fd:
            fd.write(data)


def test_migrate_retires_byte_identical_seeds():
    with tempfile.TemporaryDirectory() as t:
        src, _payload = _make_source(t)
        rt_dir = os.path.join(t, "runtime")
        _seed(rt_dir, src, ("config.json", "route.json", "adapter-map.json"))
        removed = sa.migrate_seeded_config(rt_dir, source_dir=src)
        assert sorted(removed) == ["adapter-map.json", "config.json", "route.json"]
        # all leftover seeds gone -> shipped default resolves at runtime.
        for name in ("config.json", "route.json", "adapter-map.json"):
            assert not os.path.exists(os.path.join(rt_dir, name))


def test_migrate_keeps_a_genuine_override():
    with tempfile.TemporaryDirectory() as t:
        src, _payload = _make_source(t)
        rt_dir = os.path.join(t, "runtime")
        os.makedirs(rt_dir)
        # a config the user edited (diverges from the shipped default) -> a real
        # override, must be kept.
        with open(os.path.join(rt_dir, "config.json"), "w") as fh:
            fh.write('{"mode": "propose"}\n')
        # a byte-identical route.json (leftover seed) -> retired.
        with open(os.path.join(src, "route.json"), "rb") as fs:
            route_bytes = fs.read()
        with open(os.path.join(rt_dir, "route.json"), "wb") as fd:
            fd.write(route_bytes)
        removed = sa.migrate_seeded_config(rt_dir, source_dir=src)
        assert removed == ["route.json"]
        assert os.path.isfile(os.path.join(rt_dir, "config.json"))  # override kept
        with open(os.path.join(rt_dir, "config.json")) as fh:
            assert fh.read() == '{"mode": "propose"}\n'
        assert not os.path.exists(os.path.join(rt_dir, "route.json"))
        # a second call retires nothing (only the override remains) — idempotent.
        assert sa.migrate_seeded_config(rt_dir, source_dir=src) == []


def test_migrate_noop_when_source_absent():
    with tempfile.TemporaryDirectory() as t:
        rt_dir = os.path.join(t, "runtime")
        os.makedirs(rt_dir)
        # a runtime file exists but no shipped default dir -> nothing to compare
        # against, so it is left untouched (source-tree / bare-test safety).
        with open(os.path.join(rt_dir, "config.json"), "w") as fh:
            fh.write('{"mode": "propose"}\n')
        assert sa.migrate_seeded_config(
            rt_dir, source_dir=os.path.join(t, "nope")) == []
        assert os.path.isfile(os.path.join(rt_dir, "config.json"))
