#!/usr/bin/env bash
# Start a clean, disposable Bookworm guest; Docker is installed by the proof.
set -euo pipefail
umask 077
dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
state=${1:?usage: boot.sh STATE_DIR CACHED_IMAGE}
image=${2:?usage: boot.sh STATE_DIR CACHED_IMAGE}
mkdir -p -- "$state" "$(dirname -- "$image")"
state=$(realpath -- "$state")
image=$(realpath -m -- "$image")
mapfile -t pin < "$dir/../debian-12.image"
if (( ${#pin[@]} != 2 )) ||
    [[ ! ${pin[0]} =~ ^https://cloud\.debian\.org/images/cloud/bookworm/[0-9]{8}-[0-9]+/debian-12-generic-amd64-[0-9]{8}-[0-9]+\.qcow2$ ]] ||
    [[ ! ${pin[1]} =~ ^[a-f0-9]{128}$ ]]; then
    echo "Invalid Debian image pin" >&2
    exit 1
fi
[[ -r /dev/kvm && -w /dev/kvm ]] || { echo "KVM is unavailable" >&2; exit 1; }
if [[ ! -f "$image" ]]; then
    curl --fail --silent --show-error --location --retry 3 --max-time 300 \
        --proto '=https' --proto-redir '=https' -o "$image.download" "${pin[0]}"
    printf '%s  %s\n' "${pin[1]}" "$image.download" | sha512sum --check --status
    mv -- "$image.download" "$image"
fi
printf '%s  %s\n' "${pin[1]}" "$image" | sha512sum --check
qemu-img create -q -f qcow2 -F qcow2 -b "$image" "$state/guest.qcow2" 24G
ssh-keygen -q -t ed25519 -N '' -C orama-e2e -f "$state/id_ed25519"
sed "s|@PUBLIC_KEY@|$(cat "$state/id_ed25519.pub")|" "$dir/user-data.tmpl" > "$state/user-data"
printf 'instance-id: orama-debian-proof\nlocal-hostname: orama-debian\n' > "$state/meta-data"
cloud-localds "$state/seed.iso" "$state/user-data" "$state/meta-data"
qemu-system-x86_64 -enable-kvm -cpu host -smp 4 -m 8192 -display none -no-reboot \
    -daemonize -pidfile "$state/qemu.pid" -serial "file:$state/serial.log" \
    -drive "file=$state/guest.qcow2,format=qcow2,if=virtio" \
    -drive "file=$state/seed.iso,format=raw,media=cdrom,readonly=on" \
    -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22,hostfwd=tcp:127.0.0.1:8765-:8765 \
    -device virtio-net-pci,netdev=net0
ssh_guest=(
    ssh -T -i "$state/id_ed25519" -p 2222 -o BatchMode=yes -o IdentitiesOnly=yes
    -o ConnectTimeout=4 -o StrictHostKeyChecking=accept-new
    -o "UserKnownHostsFile=$state/known_hosts" -o GlobalKnownHostsFile=/dev/null
    proof@127.0.0.1
)
deadline=$((SECONDS + 240))
until "${ssh_guest[@]}" true >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
        echo "Debian guest did not open SSH within four minutes" >&2
        tail -n 70 "$state/serial.log" >&2
        exit 1
    fi
    sleep 3
done
timeout 180 "${ssh_guest[@]}" 'sudo -n cloud-init status --wait'
# Expand the release variables on the guest.
# shellcheck disable=SC2016
timeout 30 "${ssh_guest[@]}" 'sudo -n true && . /etc/os-release && test "$ID:$VERSION_ID" = debian:12'
