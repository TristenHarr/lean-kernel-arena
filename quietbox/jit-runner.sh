#!/usr/bin/env bash
# The quiet box's GitHub runner: one just-in-time registration per job, so no job inherits a runner
# another job used. Started by lka-runner.service as root; each job runs as lka-runner, never as root.
#
#   /etc/lka/github-token   a fine-grained token for the repository with Administration: read and write
#   /etc/lka/github-repo    owner/name of the repository (default TristenHarr/lean-kernel-arena)
set -euo pipefail

repo="$(cat /etc/lka/github-repo 2>/dev/null || echo TristenHarr/lean-kernel-arena)"
runner=/srv/lka/runner/actions-runner
host="$(hostname -s)"

while true; do
  token="$(cat /etc/lka/github-token)"
  name="$host-$(date -u +%Y%m%dT%H%M%SZ)"
  body="$(jq -nc --arg name "$name" '{name: $name, runner_group_id: 1, labels: ["self-hosted", "linux", "x64", "lka-n100-quiet"], work_folder: "_work"}')"
  jit="$(curl -fsS -X POST -H "Authorization: Bearer $token" -H "Accept: application/vnd.github+json" \
          "https://api.github.com/repos/$repo/actions/runners/generate-jitconfig" -d "$body" | jq -r .encoded_jit_config)"
  if [ -z "$jit" ] || [ "$jit" = "null" ]; then
    echo "could not register a runner; retrying in 60 s" >&2
    sleep 60
    continue
  fi
  rm -rf "$runner/_work"
  install -d -o lka-runner -g lka-runner "$runner/_work"
  runuser -u lka-runner -- "$runner/run.sh" --jitconfig "$jit" || true
  sleep 5
done
