#!/usr/bin/env bash
set -eu
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
[[ -x ./wplc ]] || chmod u+x ./wplc

# The station release listens on TCP 102. Respect a lower namespace limit.
first=1024
if [[ -r /proc/sys/net/ipv4/ip_unprivileged_port_start ]]; then
    read -r first < /proc/sys/net/ipv4/ip_unprivileged_port_start
fi
if (( EUID != 0 && 102 < first )); then
    PATH="$PATH:/usr/sbin:/sbin"
    if ! command -v getcap >/dev/null || ! command -v setcap >/dev/null; then
        echo 'Для порта 102 нужны getcap и setcap (Debian: пакет libcap2-bin).' >&2
        exit 1
    fi
    caps=$(getcap ./wplc)
    if [[ ! $caps =~ cap_net_bind_service=([eip]*e[eip]*) ]]; then
        sudo setcap cap_net_bind_service=ep ./wplc
    fi
fi
exec ./wplc gateway program.pkg s7-map.json protocols.json "${1:-0}"
