#!/usr/bin/env python3
# rabbit-project-map.py — add a source_map entry to project-map.json
# Usage: python3 rabbit-project-map.py <project_map_path> <source_path> <feature_name>
import json, sys

path = sys.argv[1]
src = sys.argv[2]
feat = sys.argv[3]
with open(path) as f:
    data = json.load(f)
if "source_map" not in data:
    data["source_map"] = {}
data["source_map"][src] = feat
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
