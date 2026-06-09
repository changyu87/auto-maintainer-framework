#!/usr/bin/env python3
# rabbit-project-set-path.py — update "path" field in project-map.json
# Usage: python3 rabbit-project-set-path.py <project_map_path> <new_path>
import json, sys

path = sys.argv[1]
new_val = sys.argv[2]
with open(path) as f:
    data = json.load(f)
data["path"] = new_val
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
