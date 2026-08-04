#!/usr/bin/env python3
"""rabbit-issue: list issues with type / feature / status filters.

Output is sorted by issue number ascending so downstream callers can
diff/grep deterministically.

Selection is ACTIONABILITY-based: only issues carrying a valid
`feature:<name>` label are listed (the basis the queue adopted in
coexistence step 1 of #753, #758). It is no longer keyed on the retired
`rabbit-managed` label.

Version: 1.1.0
Owner: rabbit-workflow team
Deprecation criterion: when rabbit-issue is retired
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _gh import repo_slug, require_auth  # noqa: E402

VALID_TYPES = ("bug", "enhancement", "all")
VALID_STATES = ("open", "closed", "all")


def _label_value(issue: dict, prefix: str) -> str:
    for lbl in issue.get("labels", []):
        if lbl["name"].startswith(prefix):
            return lbl["name"][len(prefix):]
    return ""


def _issue_type(issue: dict) -> str:
    names = {lbl["name"] for lbl in issue.get("labels", [])}
    if "bug" in names:
        return "bug"
    if "enhancement" in names:
        return "enhancement"
    return "?"


def _is_actionable(issue: dict) -> bool:
    """An issue is actionable iff it carries a valid `feature:<name>` label.

    Actionability is the selection basis adopted by the queue in
    coexistence step 1 of #753 (#758); listing is no longer keyed on the
    retired `rabbit-managed` label. A raw, hand-filed GitHub issue with no
    `feature:` label is excluded.
    """
    for lbl in issue.get("labels", []):
        name = lbl["name"]
        if name.startswith("feature:") and name.split(":", 1)[1]:
            return True
    return False


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--type", default="all", choices=VALID_TYPES)
    p.add_argument("--feature", default="")
    p.add_argument("--status", default="open", choices=VALID_STATES)
    args = p.parse_args()

    require_auth()

    cmd = ["gh", "issue", "list", "-R", repo_slug(),
           "--state", args.status,
           "--limit", "500",
           "--json", "number,title,state,labels"]
    if args.type != "all":
        cmd += ["--label", args.type]
    if args.feature:
        cmd += ["--label", "feature:{}".format(args.feature)]

    out = subprocess.check_output(cmd, text=True)
    issues = json.loads(out)
    # Actionability filter: only issues carrying a valid `feature:` label.
    issues = [i for i in issues if _is_actionable(i)]
    issues.sort(key=lambda i: i["number"])
    for i in issues:
        feature = _label_value(i, "feature:")
        priority = _label_value(i, "priority:")
        print("#{}  [{}]  [{}]  [{}]  feature:{}  {}".format(
            i["number"], _issue_type(i), i["state"], priority, feature, i["title"]
        ))


if __name__ == "__main__":
    main()
