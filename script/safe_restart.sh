#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source "${script_dir}/safe_process_common.sh"

usage() {
  cat <<'EOF'
Usage:
  ./script/safe_restart.sh        # choose one live process interactively
  ./script/safe_restart.sh --all  # restart all running QuantAda live supervisors
EOF
}

fd_target_path() {
  local pid=$1
  local fd=$2
  local target

  target=$(readlink "/proc/${pid}/fd/${fd}" 2>/dev/null) || return 1
  target=${target% (deleted)}
  case "$target" in
    /dev/pts/*|/dev/tty*)
      return 1
      ;;
    pipe:*|socket:*|anon_inode:*)
      return 1
      ;;
  esac
  printf '%s' "$target"
}

process_cwd() {
  local pid=$1
  readlink "/proc/${pid}/cwd" 2>/dev/null || pwd -P
}

start_nohup() {
  local cwd=$1
  local stdout_path=$2
  local stderr_path=$3
  shift 3

  (
    if [[ -n ${qada_operation_lock_fd:-} ]]; then
      exec {qada_operation_lock_fd}>&-
    fi
    cd "$cwd" || exit 1
    if [[ -n $stdout_path ]]; then
      if [[ -n $stderr_path && $stderr_path != "$stdout_path" ]]; then
        nohup "$@" >>"$stdout_path" 2>>"$stderr_path" < /dev/null &
      else
        nohup "$@" >>"$stdout_path" 2>&1 < /dev/null &
      fi
    else
      nohup "$@" > nohup.out 2>&1 < /dev/null &
    fi
    printf '%s\n' "$!"
  )
}

restart_supervisor() {
  local pid=$1
  local cwd stdout_path stderr_path rendered new_pid existing_pid target_key existing_key
  local -a argv=()
  local -a existing_argv=()

  if ! qada_read_cmdline "$pid" argv; then
    qada_out "Cannot read command line for supervisor ${pid}; skipped."
    return 1
  fi
  if ! qada_is_live_command "${argv[@]}"; then
    qada_out "Supervisor ${pid} is no longer a QuantAda live command; skipped."
    return 1
  fi

  cwd=$(process_cwd "$pid")
  stdout_path=$(fd_target_path "$pid" 1 || true)
  stderr_path=$(fd_target_path "$pid" 2 || true)
  rendered=$(qada_render_cmdline "${argv[@]}")
  target_key=$(qada_live_command_key "${argv[@]}")

  qada_out "Restarting supervisor ${pid}:"
  qada_out "  ${rendered}"
  if [[ -n $stdout_path ]]; then
    qada_out "  log stdout: ${stdout_path}"
    if [[ -n $stderr_path && $stderr_path != "$stdout_path" ]]; then
      qada_out "  log stderr: ${stderr_path}"
    fi
  else
    qada_out "  log stdout: ${cwd}/nohup.out"
  fi

  if ! qada_stop_supervisor "$pid" 30; then
    qada_out "Restart skipped to avoid starting a duplicate process."
    return 1
  fi

  # Re-scan after shutdown. A concurrent/manual launcher or a previously
  # orphaned worker may already own this exact live command.
  qada_collect_processes
  for existing_pid in "${qada_root_pids[@]}"; do
    existing_argv=()
    if ! qada_read_cmdline "$existing_pid" existing_argv; then
      continue
    fi
    existing_key=$(qada_live_command_key "${existing_argv[@]}")
    if [[ -n $target_key && $existing_key == "$target_key" ]]; then
      qada_out "Equivalent live supervisor ${existing_pid} is already running; no duplicate started."
      return 0
    fi
  done

  new_pid=$(start_nohup "$cwd" "$stdout_path" "$stderr_path" "${argv[@]}") || {
    qada_out "Failed to restart supervisor ${pid}."
    return 1
  }
  qada_out "Started new supervisor pid=${new_pid}."
  return 0
}

restart_all() {
  local failures=0
  local pid

  qada_acquire_operation_lock "process-control"
  qada_collect_processes
  if [[ ${#qada_root_pids[@]} -eq 0 ]]; then
    qada_out "No running QuantAda live supervisor processes were found."
    return 0
  fi

  for pid in "${qada_root_pids[@]}"; do
    if ! restart_supervisor "$pid"; then
      failures=$((failures + 1))
    fi
  done

  if (( failures > 0 )); then
    qada_out "${failures} restart operation(s) failed."
    return 1
  fi
  return 0
}

interactive_restart() {
  local failures=0
  local pid
  local -a selected_pids=()

  qada_require_tty "safe_restart.sh"
  qada_acquire_operation_lock "process-control"
  trap 'qada_cleanup_and_exit 0' INT TERM

  qada_collect_processes
  if [[ ${#qada_root_pids[@]} -eq 0 ]]; then
    qada_out "No running QuantAda live supervisor processes were found."
    exit 0
  fi

  qada_select_processes selected_pids "QuantAda live processes" "restart"
  for pid in "${selected_pids[@]}"; do
    if ! restart_supervisor "$pid"; then
      failures=$((failures + 1))
    fi
  done

  if (( failures > 0 )); then
    qada_out "${failures} restart operation(s) failed."
    return 1
  fi
}

case "${1:-}" in
  "")
    interactive_restart
    ;;
  --all)
    restart_all
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
