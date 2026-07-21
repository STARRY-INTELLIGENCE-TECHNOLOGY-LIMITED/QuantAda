#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source "${script_dir}/safe_process_common.sh"

qada_require_tty "safe_stop.sh"
trap 'qada_cleanup_and_exit 0' INT TERM

qada_collect_processes
if [[ ${#qada_root_pids[@]} -eq 0 ]]; then
  qada_out "No running QuantAda live supervisor processes were found."
  exit 0
fi

selected_pid=""
qada_select_process selected_pid "QuantAda live processes" "stop"
qada_stop_supervisor "$selected_pid" 30
