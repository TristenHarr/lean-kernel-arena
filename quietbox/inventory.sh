#!/usr/bin/env bash
# What a candidate quiet box is and what it is doing, read-only, as any user (root adds the memory
# layout): the processor and its counters, memory, kernel, load, every service, timer, listening port
# and logged-in user, disk, energy counters, the clock's governor and the temperatures. A box is a
# candidate when, beyond sshd and the system's own services, nothing runs, listens or is scheduled.
#
#   ssh <box> bash -s < quietbox/inventory.sh
set -uo pipefail

section() { printf '\n== %s\n' "$1"; }

section "machine"
echo "host       $(hostname)"
echo "kernel     $(uname -r) $(uname -m)"
echo "os         $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
echo "cmdline    $(cat /proc/cmdline)"
echo "uptime     $(uptime -p)"

section "processor"
grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | sed 's/^ //'
echo "cpus       $(nproc --all) online $(cat /sys/devices/system/cpu/online)"
echo "pmus       $(ls /sys/bus/event_source/devices | tr '\n' ' ')"
for f in /sys/devices/system/cpu/cpu0/cpufreq/{scaling_driver,scaling_governor,cpuinfo_min_freq,cpuinfo_max_freq,energy_performance_preference}; do
  [ -r "$f" ] && echo "$(basename "$f") $(cat "$f")"
done
echo "smt        $(cat /sys/devices/system/cpu/smt/active 2>/dev/null || echo n/a)"
echo "perf       paranoid $(cat /proc/sys/kernel/perf_event_paranoid)"

section "memory"
free -h
if [ "$(id -u)" = 0 ] && command -v dmidecode >/dev/null; then
  dmidecode -t memory | grep -E '^\s+(Size|Speed|Configured Memory Speed|Locator|Type):' | grep -v 'No Module'
else
  echo "(the memory layout needs root and dmidecode)"
fi
echo "swap       $(swapon --show --noheadings | wc -l) device(s)"
echo "thp        $(cat /sys/kernel/mm/transparent_hugepage/enabled)"

section "energy"
for d in /sys/class/powercap/intel-rapl:*; do
  [ -d "$d" ] && echo "$(basename "$d") $(cat "$d/name") readable=$([ -r "$d/energy_uj" ] && echo yes || echo no)"
done
[ -d /sys/class/powercap ] || echo "no powercap"

section "temperature"
for z in /sys/class/thermal/thermal_zone*; do
  [ -r "$z/temp" ] && echo "$(cat "$z/type") $(( $(cat "$z/temp") / 1000 )) C"
done

section "load"
cat /proc/loadavg
top -bn1 -o %CPU | sed -n '7,22p'

section "users"
who
last -n 5 2>/dev/null | head -6

section "listening"
ss -Htlnu 2>/dev/null | awk '{print $1, $5}' | sort -u

section "services running"
systemctl list-units --type=service --state=running --no-legend --no-pager | awk '{print $1}'

section "timers"
systemctl list-timers --all --no-legend --no-pager | awk '{print $(NF-1), $NF}'

section "cron"
ls /etc/cron.d 2>/dev/null; crontab -l 2>/dev/null | grep -v '^#'

section "containers and vms"
command -v docker >/dev/null && docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null
command -v virsh >/dev/null && virsh list --all 2>/dev/null

section "disk"
df -h -x tmpfs -x devtmpfs -x squashfs -x overlay
lsblk -d -o NAME,SIZE,ROTA,MODEL 2>/dev/null

section "network"
ip -4 -o addr show | awk '{print $2, $4}'
