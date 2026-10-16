#!/usr/bin/env bash
# Probe each tsh-proxy-db port; restart the corresponding service if dead.
#
# Why this exists: `tsh proxy db --tunnel` occasionally drops its socket
# (cert refresh interaction, network blip, OOM). systemd reports the service
# as `active` but psql can't connect. Path-unit-on-cert-change covers the
# cert-renewal case; this timer handles the silent socket-died case.
#
# Idempotent: if all ports respond, exits 0 and does nothing.
set -euo pipefail

declare -A PORTS=(
    [tsh-proxy-db]=56061
    [tsh-proxy-db-billing]=56062
    [tsh-proxy-db-plugin]=56063
)

LOG_FILE=/var/log/proxy-health.log
# 让出 watchdog/path-unit 的 grace 期：cert 在 GRACE 秒内被续期过则跳过本轮探测
# （path-unit 自己会 restart proxies；proxy-health 不要插一脚加剧 crashloop）
CERT_FILE=${TELEPORT_CERT_FILE:-/opt/zd-agent/.tsh/keys/teleport.example.com/agent-user.crt}
GRACE_SECONDS=60

log() {
    echo "[$(date -Iseconds)] $*" | tee -a "$LOG_FILE" >&2
}

# Skip whole probe if cert was just rotated; let tsh-cert.path handle restart.
if [ -f "$CERT_FILE" ]; then
    cert_age=$(( $(date +%s) - $(stat -c %Y "$CERT_FILE") ))
    if [ "$cert_age" -lt "$GRACE_SECONDS" ]; then
        log "cert rotated $cert_age s ago (< ${GRACE_SECONDS}s); skipping probe — path-unit owns restart"
        exit 0
    fi
fi

probe() {
    # Returns 0 if port is reachable + responds (TCP handshake completes).
    timeout 3 bash -c "</dev/tcp/127.0.0.1/$1" 2>/dev/null
}

restart_count=0
for svc in "${!PORTS[@]}"; do
    port="${PORTS[$svc]}"
    if probe "$port"; then
        continue
    fi
    log "$svc port $port unreachable; restarting"
    if systemctl restart "$svc"; then
        log "$svc restarted OK"
        restart_count=$((restart_count + 1))
    else
        log "$svc restart FAILED (rc=$?)"
    fi
done

if [ "$restart_count" -gt 0 ]; then
    log "summary: restarted $restart_count proxy service(s)"
fi
