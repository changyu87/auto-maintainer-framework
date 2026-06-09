"""generate_readme — verbatim copy of README.md template into a target path.

Inv 3 of the rabbit-meta feature. Pure stdlib; idempotent via byte-equality.

Version: 1.0.0
Owner: rabbit-workflow team
Deprecation criterion: inherits from rabbit-meta feature deprecation
    (when rabbit's per-project plugin model is superseded by a native
    Claude Code workflow contract mechanism).
"""

import os


def generate_readme(template_path: str, output_path: str) -> str:
    """Copy template_path verbatim to output_path; idempotent.

    Returns the literal string "no-op" if output_path already exists with
    content byte-identical to the template (no write occurs), otherwise
    writes the content and returns the literal string "wrote".

    Raises FileNotFoundError if template_path does not exist.
    """
    with open(template_path, "rb") as f:
        template_bytes = f.read()
    if os.path.isfile(output_path):
        with open(output_path, "rb") as f:
            existing = f.read()
        if existing == template_bytes:
            return "no-op"
    with open(output_path, "wb") as f:
        f.write(template_bytes)
    return "wrote"
