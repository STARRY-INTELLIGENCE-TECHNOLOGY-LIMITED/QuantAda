#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source "${script_dir}/safe_process_common.sh"

usage() {
  cat <<'EOF'
Usage:
  ./script/safe_stop.sh        # choose one live process interactively
  ./script/safe_stop.sh --all  # stop all running QuantAda live supervisors
EOF
}

stop_all() {
  local failures=0
  local pid

  qada_collect_processes
  if [[ ${#qada_root_pids[@]} -eq 0 ]]; then
    qada_out "No running QuantAda live supervisor processes were found."
    return 0
  fi

  for pid in "${qada_root_pids[@]}"; do
    if ! qada_stop_supervisor "$pid" 30; then
      failures=$((failures + 1))
    fi
  done

  if (( failures > 0 )); then
    qada_out "${failures} stop operation(s) failed."
    return 1
  fi
  return 0
}

interactive_stop() {
  local selected_pid=""

  qada_require_tty "safe_stop.sh"
  trap 'qada_cleanup_and_exit 0' INT TERM

  qada_collect_processes
  if [[ ${#qada_root_pids[@]} -eq 0 ]]; then
    qada_out "No running QuantAda live supervisor processes were found."
    exit 0
  fi

  qada_select_process selected_pid "QuantAda live processes" "stop"
  qada_stop_supervisor "$selected_pid" 30
}

if [[ $# -gt 1 ]]; then
  usage >&2
  exit 2
fi

case "${1:-}" in
  "")
    interactive_stop
    ;;
  --all)
    stop_all
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
