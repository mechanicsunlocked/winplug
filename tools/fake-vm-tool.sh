#!/bin/bash
# Stand-in for /usr/bin/omarchy-windows-vm in tests.  Records how the daemon
# called it (arguments and the environment it was given) next to itself, and
# pretends to start or stop.  Touch fake-vm.fail beside it to make it fail.
log="${0%/*}/fake-vm.log"
{
    printf 'argv:'; printf ' %s' "$@"; printf '\n'
    printf 'env: PKEXEC_UID=%s HOME=%s LC_ALL=%s PATH=%s\n' "${PKEXEC_UID:-}" "${HOME:-}" "${LC_ALL:-}" "${PATH:-}"
} >>"$log"
[[ ${1:-} == __priv ]] || { echo "fake: expected __priv, got '${1:-}'" >&2; exit 1; }
if [[ -e ${0%/*}/fake-vm.fail ]]; then
    echo "fake: cannot $2 right now" >&2
    exit 1
fi
case ${2:-} in
up) echo "fake: started" ;;
down) echo "fake: stopped" ;;
*) echo "fake: unknown action '${2:-}'" >&2; exit 1 ;;
esac
