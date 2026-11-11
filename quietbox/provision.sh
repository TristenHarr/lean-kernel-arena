#!/usr/bin/env bash
# Make a fresh Ubuntu 24.04 machine into the quiet box, from a checkout of this repository:
#
#   sudo quietbox/provision.sh machines/n100-quiet.yaml
#
# Idempotent: run it again after pulling a new revision to reinstall the helper. It installs perf for
# the running kernel, process accounting, multi-user Nix with flakes (the arena's flake builds every
# checker), the runner's and the checkers' accounts, the state under /srv/lka, the root helpers with a
# sudoers file naming exactly what the runner may start, the checkers' egress policy, the build and run
# slices and the GitHub runner service; then it runs the measurement selftest and refuses to finish
# unless every check passes. It does not pin the clock or write the epoch: that is quietbox/calibrate.py's.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then echo "run as root" >&2; exit 1; fi
machine_yaml="${1:?usage: provision.sh machines/<machine>.yaml}"
here="$(cd "$(dirname "$0")/.." && pwd)"
lib=/usr/local/lib/lka
libexec=/usr/local/libexec/lka
state=/srv/lka

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  "linux-tools-$(uname -r)" linux-tools-common acct gcc libc6-dev python3 python3-yaml rsync nftables \
  jq curl git ca-certificates xz-utils dmidecode util-linux >/dev/null

echo "== nix"
if ! command -v nix >/dev/null && [ ! -x /nix/var/nix/profiles/default/bin/nix ]; then
  curl -fsSL https://nixos.org/nix/install -o /tmp/nix-install
  sh /tmp/nix-install --daemon --yes
fi
install -d /etc/nix
grep -q '^experimental-features' /etc/nix/nix.conf 2>/dev/null || echo 'experimental-features = nix-command flakes' >> /etc/nix/nix.conf
systemctl restart nix-daemon

echo "== accounts and state"
getent group lka-checkers >/dev/null || groupadd --system lka-checkers
input_group="$(python3 -c 'import json, sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["sandbox"]["input_group"])' "$machine_yaml")"
getent group "$input_group" >/dev/null || groupadd --system "$input_group"
id lka-runner >/dev/null 2>&1 || useradd --system --create-home --home-dir "$state/runner" --shell /bin/bash lka-runner
install -d -o root -g root -m 0755 "$state" "$state/checkers" "$state/run"
# The staged inputs: written by the runner, readable by the input group alone (a measured run is
# given it, a build never is); new files and shards inherit the group.
install -d -o lka-runner -g "$input_group" -m 2750 "$state/tests"
chgrp -R "$input_group" "$state/tests"
find "$state/tests" -mindepth 1 -type d -exec chmod 2750 {} +
find "$state/tests" -type f -exec chmod 0640 {} +
install -d -o lka-runner -g lka-runner -m 0755 "$state/results" "$state/arena" "$state/published" "$state/exports"
install -d -o root -g root -m 0700 /etc/lka

echo "== helpers"
install -d "$lib" "$libexec"
rm -rf "$lib/lka_quiet"
cp -r "$here/lka_quiet" "$lib/lka_quiet"
find "$lib" -name __pycache__ -prune -exec rm -rf {} +
gcc -O2 -Wall -Wextra -Werror -std=gnu11 -o "$libexec/lka-shim" "$here/quietbox/lka-shim.c"
install -m 0755 "$here/quietbox/lka-measure" "$here/quietbox/lka-quiet" "$here/quietbox/lka-preflight" "$libexec/"
chmod 0755 "$libexec/lka-shim"
git -C "$here" rev-parse HEAD > "$libexec/VERSION"
python3 -c 'import json, sys, yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))' "$machine_yaml" > /etc/lka/machine.json
sed -n 's/.*"rev": *"\([0-9a-f]*\)".*/\1/p' "$here/flake.lock" | head -1 > /etc/lka/nixpkgs-rev
chmod 0644 /etc/lka/machine.json /etc/lka/nixpkgs-rev

echo "== sudoers"
cat > /etc/sudoers.d/lka.tmp <<EOF
lka-runner ALL=(root) NOPASSWD: $libexec/lka-measure, $libexec/lka-measure --build, $libexec/lka-measure --remove, $libexec/lka-quiet on, $libexec/lka-quiet off, $libexec/lka-quiet status, $libexec/lka-preflight --check
EOF
visudo -cf /etc/sudoers.d/lka.tmp
mv /etc/sudoers.d/lka.tmp /etc/sudoers.d/lka
chmod 0440 /etc/sudoers.d/lka

echo "== checkers' egress"
gid="$(getent group lka-checkers | cut -d: -f3)"
python3 -c "import sys; sys.path.insert(0, '$lib'); from lka_quiet.build import nft_ruleset; print(nft_ruleset($gid))" > /etc/nftables-lka.nft
grep -q 'include "/etc/nftables-lka.nft"' /etc/nftables.conf || echo 'include "/etc/nftables-lka.nft"' >> /etc/nftables.conf
nft list table inet lka_checkers >/dev/null 2>&1 && nft delete table inet lka_checkers
nft -f /etc/nftables-lka.nft
systemctl enable nftables >/dev/null

echo "== process accounting"
systemctl enable --now acct >/dev/null

echo "== slices and the runner"
cat > /etc/systemd/system/lka.slice <<'EOF'
[Unit]
Description=Quiet board measured runs
EOF
cat > /etc/systemd/system/lka-build.slice <<'EOF'
[Unit]
Description=Quiet board checker builds
EOF
install -m 0755 "$here/quietbox/jit-runner.sh" "$libexec/jit-runner.sh"
install -m 0644 "$here/quietbox/lka-runner.service" /etc/systemd/system/lka-runner.service
systemctl daemon-reload
if [ ! -x "$state/runner/actions-runner/run.sh" ]; then
  version="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name | sed 's/^v//')"
  install -d -o lka-runner -g lka-runner "$state/runner/actions-runner"
  curl -fsSL "https://github.com/actions/runner/releases/download/v$version/actions-runner-linux-x64-$version.tar.gz" \
    | runuser -u lka-runner -- tar -xz -C "$state/runner/actions-runner"
  "$state/runner/actions-runner/bin/installdependencies.sh" >/dev/null
fi

echo "== measurement selftest"
python3 "$here/quietbox/selftest.py" --setup
LKA_MACHINE=/etc/lka/machine.json python3 "$here/quietbox/selftest.py"

cat <<EOF

The quiet box is provisioned. Next:
  1. sudo quietbox/calibrate.py $machine_yaml      (thermal soak, pinned clock, sentinel limits, box factor)
  2. sudo $libexec/lka-preflight --write             (record this machine's epoch)
  3. put a fine-grained token for the repository (Administration: read and write) in /etc/lka/github-token (0600)
  4. sudo systemctl enable --now lka-runner
EOF
