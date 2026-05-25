#!/usr/bin/env bash
# Benchmark: code-memory topology + retrieval vs ripgrep on the same repo.
#
# Times wall clock + counts output bytes + result counts across 4 task
# families that real coding agents run all day:
#
#   T1 — Semantic Q: "where is X handled" (cm retrieve  vs rg keyword)
#   T2 — Callers / refs of a symbol         (cm callers   vs rg symbol)
#   T3 — Definitions of a symbol            (cm defns     vs rg def-pattern)
#   T4 — Importers of a module/file         (cm importers vs rg using-stmt)
#
# Usage:
#   scripts/benchmark_vs_grep.sh <repo-path> <project-slug> <symbol> <module> "<semantic query>" [ext]
#
# Example:
#   scripts/benchmark_vs_grep.sh /path/to/repo my-project IFooService \
#       Acme.Billing.Business "where is invoice generation handled" cs
#
# Requires: code-memory ingested for <project-slug>, ripgrep (rg), python3.
set -u

REPO="${1:?repo path required}"
PROJ="${2:?project slug required}"
SYMBOL="${3:?symbol required}"
MODULE="${4:?module/file required}"
QUERY="${5:?semantic query required}"
EXT="${6:-cs}"

OUTDIR="$(mktemp -d -t cm_bench_XXX)"
trap 'echo; echo "Output samples in $OUTDIR"' EXIT

run() {
  local label="$1"; shift
  local outfile="$OUTDIR/${label// /_}.out"
  local t0 t1 bytes lines dt
  t0=$(python3 -c 'import time;print(time.perf_counter())')
  "$@" >"$outfile" 2>&1 || true
  t1=$(python3 -c 'import time;print(time.perf_counter())')
  bytes=$(wc -c <"$outfile" | tr -d ' ')
  lines=$(wc -l <"$outfile" | tr -d ' ')
  dt=$(python3 -c "print(round($t1-$t0,3))")
  printf "%-46s %8ss  %10s B  %6s lines\n" "$label" "$dt" "$bytes" "$lines"
}

cd "$REPO" || exit 1

echo "=== code-memory ($PROJ) vs ripgrep — $REPO ==="
echo

echo "T1 — Semantic: $QUERY"
run "cm retrieve"   code-memory retrieve -p "$PROJ" --json --k 8 "$QUERY"
run "rg keyword"    rg -l --type "$EXT" "$(echo "$QUERY" | tr ' ' '|')"

echo
echo "T2 — Callers + refs of $SYMBOL"
run "cm callers"    code-memory callers -p "$PROJ" --json "$SYMBOL"
run "rg symbol"     rg -l --type "$EXT" "\\b$SYMBOL\\b"

echo
echo "T3 — Definitions of $SYMBOL"
run "cm definitions" code-memory definitions -p "$PROJ" --json "$SYMBOL"
run "rg def-pattern" rg -n --type "$EXT" "(interface|class|def|function|fn|type|struct)\\s+$SYMBOL\\b"

echo
echo "T4 — Importers of $MODULE"
run "cm importers"  code-memory importers -p "$PROJ" --json "$MODULE"
MODULE_LAST=$(basename "$MODULE" | awk -F'[./]' '{print $NF}')
run "rg using-stmt" rg -l --type "$EXT" "(using|import|from)[^\\n]*\\b${MODULE_LAST}\\b"

echo
echo "=== Quality (counts parsed from JSON / line counts) ==="
python3 - <<PY
import json, pathlib
out = pathlib.Path("$OUTDIR")
def load(name):
    p = out / f"{name}.out"
    try:
        return json.loads(p.read_text())
    except Exception:
        return None
def lines(name):
    p = out / f"{name}.out"
    return sum(1 for _ in p.open()) if p.exists() else 0

t1a = load("cm_retrieve");      t1b = lines("rg_keyword")
t2a = load("cm_callers");       t2b = lines("rg_symbol")
t3a = load("cm_definitions");   t3b = lines("rg_def-pattern")
t4a = load("cm_importers");     t4b = lines("rg_using-stmt")
fmt = "%-30s %12s  %12s"
print(fmt % ("Task", "code-memory", "ripgrep"))
print(fmt % ("-"*30, "-"*12, "-"*12))
print(fmt % ("T1 semantic hits", len(t1a["code"]) if t1a else "n/a", f"{t1b} files"))
print(fmt % ("T2 callers/refs",  len(t2a["callers"]) if t2a else "n/a", f"{t2b} files"))
print(fmt % ("T3 definitions",   len(t3a["definitions"]) if t3a else "n/a", f"{t3b} matches"))
print(fmt % ("T4 importers",     len(t4a["importers"]) if t4a else "n/a", f"{t4b} files"))
PY
