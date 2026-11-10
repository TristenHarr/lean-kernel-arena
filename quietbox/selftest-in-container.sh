#!/usr/bin/env bash
# The measurement chain's acceptance test on any Linux machine with Docker: a throwaway
# privileged Ubuntu 24.04 container gets perf for the host's kernel, the shim and the helper,
# and runs quietbox/selftest.py against them without systemd. Nothing outside the container is
# changed. Timing checks are not meaningful on a busy host; every functional check is.
#
#   quietbox/selftest-in-container.sh
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
kernel="$(uname -r)"

exec docker run --rm --privileged --cgroupns=private \
  -v "$here:/lka:ro" \
  -e KERNEL="$kernel" \
  ubuntu:24.04 bash -euo pipefail -c '
    mkdir -p /sys/fs/cgroup/init
    for pid in $(cat /sys/fs/cgroup/cgroup.procs); do echo "$pid" > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || true; done
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends python3 gcc libc6-dev acct "linux-tools-$KERNEL" linux-tools-common >/dev/null
    install -d /usr/local/libexec/lka /usr/local/lib/lka
    gcc -O2 -Wall -Wextra -Werror -std=gnu11 -o /usr/local/libexec/lka/lka-shim /lka/quietbox/lka-shim.c
    cp -r /lka/lka_quiet /usr/local/lib/lka/
    install -m 755 /lka/quietbox/lka-measure /usr/local/libexec/lka/lka-measure
    python3 /lka/quietbox/selftest.py --setup
    python3 /lka/quietbox/selftest.py --no-systemd
  '
