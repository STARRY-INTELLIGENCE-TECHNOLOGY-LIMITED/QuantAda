#!/usr/bin/env bash

qada_die() {
  printf '%s\n' "$*" >&2
  exit 1
}

qada_tty_open=0

qada_require_tty() {
  local script_name=${1:-script}
  if exec 3</dev/tty 4>/dev/tty; then
    qada_tty_open=1
    return 0
  fi
  qada_die "${script_name} needs an interactive terminal."
}

qada_out() {
  if [[ ${qada_tty_open:-0} -eq 1 ]]; then
    printf '%s\n' "$*" >&4
  else
    printf '%s\n' "$*"
  fi
}

qada_cleanup_and_exit() {
  if [[ ${qada_tty_open:-0} -eq 1 ]]; then
    printf '\n' >&4
  fi
  exit "${1:-0}"
}

declare -gA qada_parent_of=()
declare -gA qada_etimes_of=()
declare -gA qada_cmdline_of=()
declare -gA qada_candidate_of=()
declare -gA qada_root_of=()
declare -gA qada_root_seen=()
declare -gA qada_children_by_root=()
declare -gA qada_root_cmdline=()
declare -ga qada_candidate_pids=()
declare -ga qada_root_pids=()
declare -ga qada_visible_pids=()

qada_reset_process_cache() {
  qada_parent_of=()
  qada_etimes_of=()
  qada_cmdline_of=()
  qada_candidate_of=()
  qada_root_of=()
  qada_root_seen=()
  qada_children_by_root=()
  qada_root_cmdline=()
  qada_candidate_pids=()
  qada_root_pids=()
  qada_visible_pids=()
}

qada_read_cmdline() {
  local pid=$1
  local -n out_ref=$2
  local path="/proc/${pid}/cmdline"

  [[ -r "$path" ]] || return 1
  mapfile -d '' -t out_ref < "$path" || true
  if [[ ${#out_ref[@]} -gt 0 ]]; then
    local last_index=$(( ${#out_ref[@]} - 1 ))
    if [[ -z ${out_ref[$last_index]} ]]; then
      unset 'out_ref[last_index]'
    fi
  fi
  [[ ${#out_ref[@]} -gt 0 ]]
}

qada_is_live_command() {
  local arg has_run=0 has_connect=0
  for arg in "$@"; do
    case "$arg" in
      */run.py|run.py) has_run=1 ;;
      --connect|--connect=*) has_connect=1 ;;
    esac
  done
  [[ $has_run -eq 1 && $has_connect -eq 1 ]]
}

qada_render_cmdline() {
  local arg rendered=""
  for arg in "$@"; do
    if [[ -n $rendered ]]; then
      rendered+=" "
    fi
    if [[ $arg == *[[:space:]]* ]]; then
      rendered+="$(printf '%q' "$arg")"
    else
      rendered+="$arg"
    fi
  done
  printf '%s' "$rendered"
}

qada_resolve_root_pid() {
  local pid=$1
  local current=$pid
  local parent
  local guard=0

  while :; do
    parent=${qada_parent_of[$current]:-}
    [[ -n $parent ]] || break
    [[ ${qada_candidate_of[$parent]:-0} -eq 1 ]] || break
    current=$parent
    guard=$((guard + 1))
    [[ $guard -gt 64 ]] && break
  done

  printf '%s' "$current"
}

qada_collect_processes() {
  local pid ppid etimes root
  local -a argv=()

  qada_reset_process_cache
  while read -r pid ppid etimes; do
    [[ $pid =~ ^[0-9]+$ ]] || continue
    [[ $ppid =~ ^[0-9]+$ ]] || continue
    [[ $etimes =~ ^[0-9]+$ ]] || continue

    if ! qada_read_cmdline "$pid" argv; then
      continue
    fi
    if ! qada_is_live_command "${argv[@]}"; then
      continue
    fi

    qada_parent_of["$pid"]=$ppid
    qada_etimes_of["$pid"]=$etimes
    qada_cmdline_of["$pid"]=$(qada_render_cmdline "${argv[@]}")
    qada_candidate_of["$pid"]=1
    qada_candidate_pids+=("$pid")
  done < <(ps -ww -eo pid=,ppid=,etimes= --sort=-etimes)

  for pid in "${qada_candidate_pids[@]}"; do
    root=$(qada_resolve_root_pid "$pid")
    qada_root_of["$pid"]=$root
    if [[ -z ${qada_root_seen[$root]:-} ]]; then
      qada_root_seen["$root"]=1
      qada_root_pids+=("$root")
      qada_root_cmdline["$root"]=${qada_cmdline_of["$root"]}
    fi
  done

  for pid in "${qada_candidate_pids[@]}"; do
    root=${qada_root_of[$pid]}
    if [[ $pid != "$root" ]]; then
      if [[ -n ${qada_children_by_root[$root]:-} ]]; then
        qada_children_by_root["$root"]+=",${pid}"
      else
        qada_children_by_root["$root"]=$pid
      fi
    fi
  done
}

qada_filter_processes() {
  local query=${1,,}
  local pid command

  qada_visible_pids=()
  for pid in "${qada_root_pids[@]}"; do
    command=${qada_root_cmdline[$pid],,}
    if [[ -z $query || $command == *"$query"* ]]; then
      qada_visible_pids+=("$pid")
    fi
  done
}

qada_render_menu() {
  local title=$1
  local hint=$2
  local selected=$3
  local status=$4
  local query=$5
  local idx root marker children

  printf '\033[H\033[J' >&4
  printf '%s\n' "$title" >&4
  printf '%s\n\n' "$hint" >&4

  if [[ -n $query ]]; then
    printf 'Command filter: %s\n\n' "$query" >&4
  fi

  for idx in "${!qada_visible_pids[@]}"; do
    root=${qada_visible_pids[$idx]}
    marker=' '
    if [[ $idx -eq $selected ]]; then
      marker='>'
    fi
    children=${qada_children_by_root[$root]:-}
    if [[ -n $children ]]; then
      printf '%s%2d) supervisor=%s children=%s\n    %s\n\n' \
        "$marker" "$((idx + 1))" "$root" "$children" \
        "${qada_root_cmdline[$root]}" >&4
    else
      printf '%s%2d) supervisor=%s\n    %s\n\n' \
        "$marker" "$((idx + 1))" "$root" "${qada_root_cmdline[$root]}" >&4
    fi
  done

  if [[ ${#qada_visible_pids[@]} -eq 0 ]]; then
    printf 'No commands match the current filter.\n\n' >&4
  fi

  if [[ -n $status ]]; then
    printf '%s\n' "$status" >&4
  fi
}

qada_select_processes() {
  local -n out_pids=$1
  local title=$2
  local action=$3
  local selected_index=0
  local typed=""
  local status=""
  local query=""
  local key rest choice answer

  out_pids=()
  qada_filter_processes ""

  while :; do
    qada_render_menu \
      "$title" \
      "Up/Down move, digits select, / search command, a select all, Enter ${action}, q cancel" \
      "$selected_index" \
      "$status" \
      "$query"
    status=""

    key=""
    if ! IFS= read -rsn1 key <&3; then
      qada_cleanup_and_exit 0
    fi

    if [[ $key == $'\e' ]]; then
      rest=""
      if IFS= read -rsn2 -t 0.05 rest <&3; then
        key+="$rest"
      fi
    fi

    case "$key" in
      ""|$'\n'|$'\r')
        if [[ $typed == "a" || $typed == "A" ]]; then
          if [[ ${#qada_visible_pids[@]} -eq 0 ]]; then
            status="No matching processes to ${action}."
            typed=""
            continue
          fi

          qada_render_menu \
            "$title" \
            "Confirm selection before ${action}" \
            "$selected_index" \
            "This will ${action} all ${#qada_visible_pids[@]} listed supervisor(s)." \
            "$query"
          printf 'Confirm %s all listed supervisors? [y/N] ' "$action" >&4
          answer=""
          if ! IFS= read -r answer <&3; then
            qada_cleanup_and_exit 0
          fi
          answer=${answer%$'\r'}
          if [[ $answer == "y" || $answer == "Y" ]]; then
            out_pids=("${qada_visible_pids[@]}")
            return 0
          fi
          typed=""
          status="All selection cancelled."
          continue
        elif [[ -n $typed ]]; then
          choice=$((10#$typed - 1))
        else
          choice=$selected_index
        fi
        if (( choice < 0 || choice >= ${#qada_visible_pids[@]} )); then
          status="Invalid selection: ${typed:-$((selected_index + 1))}"
          typed=""
          continue
        fi
        out_pids=("${qada_visible_pids[$choice]}")
        return 0
        ;;
      q|Q|$'\e')
        qada_cleanup_and_exit 0
        ;;
      $'\e[A')
        selected_index=$(( selected_index > 0 ? selected_index - 1 : 0 ))
        typed=""
        ;;
      $'\e[B')
        if (( selected_index + 1 < ${#qada_visible_pids[@]} )); then
          selected_index=$((selected_index + 1))
        fi
        typed=""
        ;;
      /)
        printf '\nCommand search (empty shows all): ' >&4
        query=""
        if ! IFS= read -r query <&3; then
          qada_cleanup_and_exit 0
        fi
        query=${query%$'\r'}
        qada_filter_processes "$query"
        selected_index=0
        typed=""
        if [[ ${#qada_visible_pids[@]} -eq 0 ]]; then
          status="No command contains: ${query}"
        elif [[ -n $query ]]; then
          status="Matched ${#qada_visible_pids[@]} supervisor(s)."
        else
          status="Command filter cleared."
        fi
        ;;
      [0-9])
        if [[ $typed =~ ^[0-9]*$ ]]; then
          typed+="$key"
        else
          typed="$key"
        fi
        if [[ ${#typed} -gt 9 ]]; then
          typed=""
          status="Selection number is too large."
          continue
        fi
        if (( 10#$typed >= 1 && 10#$typed <= ${#qada_visible_pids[@]} )); then
          selected_index=$((10#$typed - 1))
          status="Selected ${typed}. Press Enter to ${action}."
        else
          status="Selected ${typed}."
        fi
        ;;
      a|A)
        typed="$key"
        if [[ ${#qada_visible_pids[@]} -gt 0 ]]; then
          status="Selected all ${#qada_visible_pids[@]} listed supervisor(s). Press Enter to continue."
        else
          status="No matching processes to ${action}."
        fi
        ;;
      $'\x7f'|$'\b')
        if [[ -n $typed ]]; then
          typed=${typed%?}
          if [[ -n $typed ]]; then
            choice=$((10#$typed - 1))
            if (( choice >= 0 && choice < ${#qada_visible_pids[@]} )); then
              selected_index=$choice
            fi
          fi
        fi
        ;;
      *)
        typed=""
        ;;
    esac
  done
}

qada_stop_supervisor() {
  local pid=$1
  local timeout_seconds=${2:-30}
  local deadline

  if ! kill -0 "$pid" 2>/dev/null; then
    qada_out "Supervisor ${pid} is already gone."
    return 0
  fi

  qada_out "Sending SIGINT to supervisor ${pid}..."
  if ! kill -INT "$pid" 2>/dev/null; then
    qada_out "Failed to send SIGINT to ${pid}."
    return 1
  fi

  deadline=$((SECONDS + timeout_seconds))
  while kill -0 "$pid" 2>/dev/null; do
    if (( SECONDS >= deadline )); then
      qada_out "SIGINT sent, but ${pid} is still running after ${timeout_seconds}s."
      return 1
    fi
    sleep 1
  done

  qada_out "Supervisor ${pid} exited cleanly."
  return 0
}
