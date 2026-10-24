#!/bin/sh
# Regenerate requirements.lock (exact versions + sha256) for the release venv.
set -eu
cd "$(dirname "$0")/.."
report=$(mktemp)
trap 'rm -f "$report"' EXIT
.venv/bin/pip install -q --dry-run --ignore-installed --only-binary :all: --report "$report" 'mcp==2.1.1'
.venv/bin/python - "$report" > requirements.lock <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
print("# Generated from `pip install --dry-run --report` for mcp==2.1.1, CPython 3.14, Linux x86_64.")
print("# Regenerate with scripts/lock.sh. install.sh installs with --require-hashes --only-binary :all:.")
for item in sorted(r["install"], key=lambda i: i["metadata"]["name"].lower()):
    name, ver = item["metadata"]["name"], item["metadata"]["version"]
    h = item["download_info"]["archive_info"]["hashes"]["sha256"]
    print(f"{name}=={ver} \\\n    --hash=sha256:{h}")
EOF
