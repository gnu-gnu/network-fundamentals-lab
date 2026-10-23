#!/bin/bash
# Own only NETLAB-* chains; never flush DOCKER-USER or global chains.
set -euo pipefail
exec 9>/run/lock/netlab-netguard.lock
flock 9
ipt() { iptables -w 10 "$@"; }
chain() { ipt -S "$1" >/dev/null 2>&1 || ipt -N "$1"; }
jump() { ipt -C "$@" 2>/dev/null || ipt -I "$@"; }
chain NETLAB-IN
chain NETLAB-FWD
# Existing own chains stay installed while iptables-restore commits the replacement.
iptables-restore -w 10 --noflush <<'RULES'
*filter
-F NETLAB-IN
-A NETLAB-IN -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
-A NETLAB-IN -p udp --dport 67 -j ACCEPT
-A NETLAB-IN -p udp --dport 53 -j ACCEPT
-A NETLAB-IN -p tcp --dport 53 -j ACCEPT
-A NETLAB-IN -p icmp -j ACCEPT
-A NETLAB-IN -j REJECT --reject-with icmp-port-unreachable
-F NETLAB-FWD
-A NETLAB-FWD -i lxdbr0 -d 10.0.0.0/8 -j REJECT --reject-with icmp-admin-prohibited
-A NETLAB-FWD -i lxdbr0 -d 172.16.0.0/12 -j REJECT --reject-with icmp-admin-prohibited
-A NETLAB-FWD -i lxdbr0 -d 192.168.0.0/16 -j REJECT --reject-with icmp-admin-prohibited
-A NETLAB-FWD -i lxdbr0 -d 169.254.0.0/16 -j REJECT --reject-with icmp-admin-prohibited
-A NETLAB-FWD -i lxdbr0 -j ACCEPT
-A NETLAB-FWD -o lxdbr0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
-A NETLAB-FWD -j RETURN
COMMIT
RULES
jump INPUT -i lxdbr0 -j NETLAB-IN
jump FORWARD -j NETLAB-FWD
if ipt -S DOCKER-USER >/dev/null 2>&1; then
  jump DOCKER-USER -j NETLAB-FWD
fi
while read -r dev; do
  [ -z "$dev" ] || bridge link set dev "$dev" isolated on
done < <(ip -o link show master lxdbr0 | awk '{gsub(/:/,"",$2); sub(/@.*/,"",$2); print $2}')
