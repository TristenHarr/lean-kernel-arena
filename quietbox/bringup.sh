#!/usr/bin/env bash
# Bring the quiet box from provisioned to measuring, in order, from a checkout of this repository:
#
#   sudo quietbox/bringup.sh machines/n100-quiet.yaml            # every phase, in order
#   sudo quietbox/bringup.sh machines/n100-quiet.yaml stage      # one phase (stage|calibrate|epoch|runner|...)
#
# provision.sh leaves the box ready; this runs what must happen on the box before the runner is
# switched on and the import is dispatched, and nothing a workflow already does for itself. Each phase
# is idempotent: run again after a new revision and only the work not yet done is redone. The phases:
#
#   verifier  build Lean's own kernel, which decides whether an invalid twin is rejected
#   seed      draw this calendar month's round seed (0600, published when the round ends)
#   published fetch the Arena's results.json and test tarball
#   exports   export the round's tests no tarball or staged export holds, with the Arena's own recipe
#   stage     re-encode and stage the round's inputs from the seed; writes the round manifest
#   official  build the reference checker, so calibration has something to time
#   calibrate soak for the clock, then the sentinel band, the box factor and the noise floor; write
#             the decisions into the profile (COMMIT it afterwards; it holds every run to them)
#   epoch     record the machine's fingerprint and epoch
#   runner    enable the GitHub runner (needs the token already in place)
#
# calibrate and runner change the machine (the clock, a system service); the rest only write files
# under /srv/lka. calibrate's soak alone holds a chosen clock for two hours, so the whole run is long.
# It never runs in a container: it sets the host's clocks. After calibrate, commit the profile and run
# `epoch` again, because the profile is part of the fingerprint.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then echo "run as root" >&2; exit 1; fi
machine_yaml="${1:?usage: bringup.sh machines/<machine>.yaml [phase]}"
phase="${2:-all}"
here="$(cd "$(dirname "$0")/.." && pwd)"
cd "$here"

state="$(python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["paths"]["state"])' "$machine_yaml")"
season="${LKA_SEASON:-scoring/season-1.yaml}"
runner_user=lka-runner
round="$(date -u +%Y-%m)"
verifier_kernel="$state/verifier/.lake/build/bin/kernel"
manifest="$state/rounds/$round/manifest.json"
published="$state/published"

# The file-writing phases are the runner's own work, so they run as it, through the flake's shell,
# from this checkout (sudo keeps the working directory). The runner must be able to read the
# checkout, exactly as it does the one a job checks out; otherwise say so instead of failing deep in a
# phase. Checked once, only when a runner-user phase is about to run.
runner_checked=""
need_runner() {
  [ -n "$runner_checked" ] && return
  sudo -u "$runner_user" test -r "$here/flake.nix" ||
    { echo "$runner_user cannot read this checkout ($here); clone it where the runner can read it (e.g. under $state) and re-run" >&2; exit 1; }
  runner_checked=yes
}
as_runner() { need_runner; sudo -u "$runner_user" nix develop --command "$@"; }
step() { printf '\n== %s\n' "$1"; }

phase_verifier() {
  step "verifier (Lean's kernel, to reject invalid twins)"
  install -o "$runner_user" -g "$runner_user" -d "$state/verifier"
  rsync -a --delete checkers/official/ "$state/verifier/"
  chown -R "$runner_user:$runner_user" "$state/verifier"
  if [ -x "$verifier_kernel" ]; then echo "already built: $verifier_kernel"; return; fi
  # nix develop runs from this checkout (the flake lives here); lake build runs in the verifier tree,
  # exactly as the quiet-round action does it.
  as_runner bash -c 'cd "'"$state"'/verifier" && lake build'
}

phase_seed() {
  step "seed ($round)"
  install -o "$runner_user" -g "$runner_user" -d -m 0700 "$state/rounds/$round"
  if [ -s "$state/rounds/$round/seed" ]; then echo "seed already drawn"; return; fi
  sudo -u "$runner_user" bash -c 'umask 077 && od -An -N32 -tx1 /dev/urandom | tr -d " \n" > "'"$state/rounds/$round"'/seed"'
  echo "drew the round seed"
}

phase_published() {
  step "published round (Arena results.json and tests)"
  install -o "$runner_user" -g "$runner_user" -d -m 0755 "$published"
  sudo -u "$runner_user" curl -fsSL -o "$published/results.json" https://arena.lean-lang.org/results.json
  sudo -u "$runner_user" curl -fsSL -o "$published/lean-arena-tests.tar.gz" https://arena.lean-lang.org/lean-arena-tests.tar.gz
  echo "fetched the published round"
}

phase_exports() {
  step "export the round's tests no export holds"
  mapfile -t missing < <(as_runner python3 lka.py quiet-missing --published "$published" \
    --source "$state/exports" --allow-mismatch cedar)
  if [ "${#missing[@]}" -eq 0 ]; then echo "every test is held by the tarball or an export"; return; fi
  echo "exporting ${missing[*]}"
  as_runner python3 lka.py build-test "${missing[@]}"
  for name in "${missing[@]}"; do
    install -o "$runner_user" -g "$runner_user" -D -m 0644 "_build/tests/$name.ndjson" "$state/exports/$name.ndjson"
  done
}

phase_stage() {
  step "stage the round's inputs"
  if [ -s "$manifest" ] && python3 -c 'import json, sys; m = json.load(open(sys.argv[1])); r = json.load(open(sys.argv[2])); sys.exit(m["arena_revision"] != r["meta"]["git_revision"])' "$manifest" "$published/results.json"; then
    echo "the round's inputs are staged for this arena revision"
    return
  fi
  as_runner python3 lka.py quiet-stage --published "$published" \
    --source "$state/exports" --store "$state/tests" --manifest "$manifest" --allow-mismatch cedar \
    --round-seed "$state/rounds/$round/seed" --season "$season" --verify-with "$verifier_kernel"
}

phase_official() {
  step "build the reference checker"
  rsync -a --delete --exclude=.git --exclude=_build --exclude=_out --exclude=_results ./ "$state/arena/"
  chown -R "$runner_user:$runner_user" "$state/arena"
  as_runner python3 lka.py quiet-build --checker official --arena "$state/arena" --results "$state/results"
}

phase_calibrate() {
  step "calibrate (long: the soak holds a clock for two hours)"
  if python3 -c 'import sys, yaml; m = yaml.safe_load(open(sys.argv[1])); sys.exit(0 if m["frequency"]["pinned_hz"] and m["timeouts"]["box_factor"] and m["sentinel"]["limits"] else 1)' "$machine_yaml"; then
    echo "the profile is already calibrated; delete its calibrated values to redo"
    return
  fi
  local out="$state/calibration"
  quietbox/calibrate.py soak "$machine_yaml"
  local khz
  khz="$(python3 -c 'import glob, json; f = sorted(glob.glob("'"$out"'/soak-*.json"))[-1]; print(json.load(open(f))["chosen_khz"])')"
  [ "$khz" = None ] && { echo "no clock held; improve cooling or lower the power limit" >&2; exit 1; }
  quietbox/calibrate.py sentinel "$machine_yaml" --pinned-khz "$khz" --manifest "$manifest"
  quietbox/calibrate.py box "$machine_yaml" --pinned-khz "$khz" --manifest "$manifest" --published "$published/results.json"
  quietbox/calibrate.py noise "$machine_yaml" --pinned-khz "$khz" --manifest "$manifest" --label stock
  local report="$out/report.json"
  python3 - "$out" "$khz" "$report" <<'PY'
import glob, json, sys
out, khz, report = sys.argv[1], int(sys.argv[2]), sys.argv[3]
latest = lambda g: json.load(open(sorted(glob.glob(f"{out}/{g}-*.json"))[-1]))
json.dump({"pinned_khz": khz,
           "box_factor": latest("box-factor")["box_factor"],
           "sentinel_limits": latest("sentinel")["limits"]},
          open(report, "w"), indent=1, sort_keys=True)
PY
  quietbox/calibrate.py write "$machine_yaml" --report "$report"
  echo "calibrated $machine_yaml -- COMMIT it, reinstall /etc/lka/machine.json (provision.sh), then run: bringup.sh $machine_yaml epoch"
}

phase_epoch() {
  step "record the machine's epoch"
  /usr/local/libexec/lka/lka-preflight --write
}

phase_runner() {
  step "enable the GitHub runner"
  if [ ! -s /etc/lka/github-token ]; then
    echo "put a fine-grained token for the repository (Administration: read and write) in /etc/lka/github-token (0600, root), then: bringup.sh $machine_yaml runner" >&2
    exit 1
  fi
  [ "$(stat -c '%a %U' /etc/lka/github-token)" = "600 root" ] || { echo "/etc/lka/github-token must be mode 0600 owned by root" >&2; exit 1; }
  systemctl enable --now lka-runner
  systemctl --no-pager --lines=0 status lka-runner || true
  echo "the runner is enabled; set the repository variable QUIET_BOARD to on, then dispatch the Quiet board import"
}

run() { for p in "$@"; do "phase_$p"; done; }

case "$phase" in
  all)       run verifier seed published exports stage official calibrate epoch runner ;;
  round)     run verifier seed published exports stage official ;;
  verifier|seed|published|exports|stage|official|calibrate|epoch|runner) run "$phase" ;;
  *) echo "unknown phase: $phase (verifier|seed|published|exports|stage|official|calibrate|epoch|runner|round|all)" >&2; exit 2 ;;
esac
