#!/usr/bin/env python3
"""rabbit-project.py — scaffold and maintain project directories.

Usage:
  rabbit-project.py init <name>
  rabbit-project.py set-path <name> <absolute-path>
  rabbit-project.py map <name> <source-path> <feature-name>

Exit: 0 success, 1 error, 2 bad invocation
"""

import os
import subprocess
import sys
from pathlib import Path


def usage() -> None:
    sys.stderr.write(
        "usage:\n"
        "  rabbit-project.py init <name>\n"
        "  rabbit-project.py set-path <name> <absolute-path>\n"
        "  rabbit-project.py map <name> <source-path> <feature-name>\n"
    )


def repo_root(script_dir: Path) -> Path:
    env = os.environ.get("RABBIT_ROOT")
    if env:
        return Path(env)
    try:
        out = subprocess.check_output(
            ["git", "-C", str(script_dir), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
        )
        return Path(out.decode().strip())
    except Exception:
        return script_dir.parent.parent.parent.parent


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    rroot = repo_root(script_dir)
    contract_templates = rroot / ".claude/features/contract/templates"

    args = sys.argv[1:]
    if not args:
        usage()
        return 2
    cmd = args[0]
    rest = args[1:]

    if cmd == "init":
        if not rest:
            usage(); return 2
        name = rest[0]
        project_dir = rroot / f"project-{name}"
        if project_dir.is_dir():
            sys.stderr.write(f"ERROR: project-{name} already exists at {project_dir}\n")
            return 1
        (project_dir / "features").mkdir(parents=True)
        (project_dir / "contract").mkdir(parents=True)
        map_template = contract_templates / "project-map-template.json"
        if not map_template.is_file():
            sys.stderr.write(f"ERROR: template not found: {map_template}\n")
            return 1
        text = map_template.read_text().replace("{{project_name}}", name).replace("{{absolute_source_root}}", "")
        (project_dir / "project-map.json").write_text(text)
        reg_template = contract_templates / "registry-template.json"
        if not reg_template.is_file():
            sys.stderr.write(f"ERROR: template not found: {reg_template}\n")
            return 1
        reg_text = reg_template.read_text().replace("{{owner}}", f"{name} team")
        (project_dir / "features/registry.json").write_text(reg_text)
        print(f"initialized project-{name}/")
        return 0

    if cmd == "set-path":
        if len(rest) < 2:
            usage(); return 2
        name, path = rest[0], rest[1]
        project_map = rroot / f"project-{name}/project-map.json"
        if not project_map.is_file():
            sys.stderr.write(f"ERROR: project-map.json not found: {project_map}\n")
            return 1
        if not path.startswith("/"):
            sys.stderr.write(f"ERROR: path must be absolute (start with /): {path}\n")
            return 1
        rc = subprocess.call(
            [sys.executable, str(script_dir / "rabbit-project-set-path.py"), str(project_map), path]
        )
        if rc != 0:
            return rc
        print(f"set project-{name} path to {path}")
        return 0

    if cmd == "map":
        if len(rest) < 3:
            usage(); return 2
        name, source_path, feature_name = rest[0], rest[1], rest[2]
        project_map = rroot / f"project-{name}/project-map.json"
        if not project_map.is_file():
            sys.stderr.write(f"ERROR: project-map.json not found: {project_map}\n")
            return 1
        rc = subprocess.call(
            [sys.executable, str(script_dir / "rabbit-project-map.py"),
             str(project_map), source_path, feature_name]
        )
        if rc != 0:
            return rc
        print(f"mapped {source_path} -> {feature_name} in project-{name}")
        return 0

    if cmd in ("", "-h", "--help", "help"):
        usage()
        return 2 if cmd == "" else 0

    sys.stderr.write(f"ERROR: unknown subcommand '{cmd}'\n")
    usage()
    return 2


if __name__ == "__main__":
    sys.exit(main())
