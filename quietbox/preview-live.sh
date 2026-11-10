#!/usr/bin/env bash
# The preview site from the official arena's latest published round, in one command:
#
#     quietbox/preview-live.sh [outdir]          (default _out; run inside `nix develop`)
#
# Fetches results.json, re-scores it under the season's rules (marked as a preview on every page),
# and renders the site with the quiet board at the root and the arena's own board at arena.html.
set -euo pipefail

cd "$(dirname "$0")/.."
outdir="${1:-_out}"
season="${SEASON:-scoring/season-1.yaml}"
machine="${MACHINE:-machines/n100-quiet.yaml}"

python3 quietbox/preview-from-arena.py --season "$season" --out _build/preview-board.json \
    --save _build/official-results.json
python3 lka.py build-site --outdir "$outdir" --results _build/official-results.json \
    --board _build/preview-board.json --machine "$machine" --quiet-home
