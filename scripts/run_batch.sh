#!/usr/bin/env bash
# Generic batch runner with preflight quota probe, log-silence watchdog,
# quota/API-error scan, and deterministic END_REASON + EXIT markers.
set -u

WORK_DIR="${RUN_BATCH_WORK_DIR:-}"
REPO_DIR="${RUN_BATCH_REPO_DIR:-.}"
LOG="${RUN_BATCH_LOG:-}"
PROMPT_FILE="${RUN_BATCH_PROMPT_FILE:-}"
OPENCODE_BIN="${OPENCODE_BIN:-opencode}"
MODEL="${RUN_BATCH_MODEL:-opencode/deepseek-v4.1-flash-free}"
PROBE_CMD="${RUN_BATCH_PROBE_CMD:-}"
NO_PROBE="${RUN_BATCH_NO_PROBE:-0}"
PROBE_TIMEOUT="${RUN_BATCH_PROBE_TIMEOUT:-180}"
IDLE_TIMEOUT="${RUN_BATCH_IDLE_TIMEOUT:-2700}"
POLL_INTERVAL="${RUN_BATCH_POLL_INTERVAL:-60}"
KILL_GRACE="${RUN_BATCH_KILL_GRACE:-5}"

usage() {
  cat <<'EOF'
Usage: scripts/run_batch.sh [OPTIONS] [-- COMMAND [ARG ...]]

Options:
  --work-dir <dir>        Batch work directory (default: $RUN_BATCH_WORK_DIR)
  --repo-dir <dir>        Working directory to cd into (default: .)
  --log <path>            Run log path (default: <work-dir>/run.log)
  --prompt-file <path>    Prompt file for default opencode command
  --opencode-bin <bin>    OpenCode binary name/path (default: opencode)
  --model <id>            Model identifier (default: opencode/deepseek-v4.1-flash-free)
  --probe-cmd <cmd>       Custom shell command for preflight probe
  --no-probe              Skip preflight quota probe
  --probe-timeout <sec>   Preflight probe timeout in seconds (default: 180)
  --idle-timeout <sec>    Max log silence in seconds before kill (default: 2700)
  --poll-interval <sec>   Watchdog poll interval in seconds (default: 60)
  --kill-grace <sec>      Seconds between TERM and KILL (default: 5)
  -h, --help              Show this help message and exit
EOF
}

CUSTOM_CMD=()
while [ $# -gt 0 ]; do
  case "$1" in
    --work-dir)
      WORK_DIR="${2:-}"
      shift 2
      ;;
    --repo-dir)
      REPO_DIR="${2:-}"
      shift 2
      ;;
    --log)
      LOG="${2:-}"
      shift 2
      ;;
    --prompt-file)
      PROMPT_FILE="${2:-}"
      shift 2
      ;;
    --opencode-bin)
      OPENCODE_BIN="${2:-}"
      shift 2
      ;;
    --model)
      MODEL="${2:-}"
      shift 2
      ;;
    --probe-cmd)
      PROBE_CMD="${2:-}"
      shift 2
      ;;
    --no-probe)
      NO_PROBE=1
      shift
      ;;
    --probe-timeout)
      PROBE_TIMEOUT="${2:-}"
      shift 2
      ;;
    --idle-timeout)
      IDLE_TIMEOUT="${2:-}"
      shift 2
      ;;
    --poll-interval)
      POLL_INTERVAL="${2:-}"
      shift 2
      ;;
    --kill-grace)
      KILL_GRACE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      CUSTOM_CMD=("$@")
      break
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

cd "$REPO_DIR" || {
  echo "error: cannot cd to repo-dir: $REPO_DIR" >&2
  exit 2
}

if [ -z "$LOG" ]; then
  if [ -n "$WORK_DIR" ]; then
    LOG="$WORK_DIR/run.log"
  else
    echo "error: specify --work-dir or --log" >&2
    exit 2
  fi
fi

if [ ${#CUSTOM_CMD[@]} -eq 0 ] && [ -z "$PROMPT_FILE" ] && [ -n "$WORK_DIR" ]; then
  if [ -f "$WORK_DIR/prompt.md" ]; then
    PROMPT_FILE="$WORK_DIR/prompt.md"
  else
    PROMPT_FILE="$WORK_DIR/prompt.txt"
  fi
fi

if [ ${#CUSTOM_CMD[@]} -eq 0 ]; then
  if [ -z "$PROMPT_FILE" ] || [ ! -f "$PROMPT_FILE" ]; then
    echo "error: prompt file not found: ${PROMPT_FILE:-<unset>}" >&2
    exit 2
  fi
fi

mkdir -p "$(dirname "$LOG")"

_terminate_child() {
  local pid="$1"
  local grace="$2"
  kill -TERM "$pid" 2>/dev/null || true
  pkill -TERM -P "$pid" 2>/dev/null || true
  local waited=0
  while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$grace" ]; do
    sleep 1
    waited=$((waited + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
    pkill -KILL -P "$pid" 2>/dev/null || true
  fi
}

_has_quota_error() {
  local file="$1"
  local offset="$2"
  tail -c +"$((offset + 1))" "$file" 2>/dev/null | grep -E -i -q "Free usage limit reached|AI_APICallError"
}

echo "=== launch attempt $(date -Is) ===" >> "$LOG"

if [ "$NO_PROBE" != "1" ]; then
  if [ -n "$PROBE_CMD" ]; then
    PROBE_OUTPUT=$(timeout "$PROBE_TIMEOUT" bash -c "$PROBE_CMD" 2>&1)
    PROBE_RC=$?
  else
    PROBE_OUTPUT=$(timeout "$PROBE_TIMEOUT" "$OPENCODE_BIN" run --model "$MODEL" "Reply with exactly: OK" 2>&1)
    PROBE_RC=$?
  fi
  echo "--- probe tail ---" >> "$LOG"
  printf "%s\n" "$PROBE_OUTPUT" | tail -n 5 >> "$LOG"
  if [ "$PROBE_RC" -ne 0 ] \
     || printf "%s" "$PROBE_OUTPUT" | grep -qi "Free usage limit reached" \
     || ! printf "%s" "$PROBE_OUTPUT" | grep -q "OK"; then
    echo "PROBE_FAIL $(date -Is)" >> "$LOG"
    echo "END_REASON:probe_fail" >> "$LOG"
    echo "EXIT:42" >> "$LOG"
    echo "=== run_batch.sh end $(date -Is) ===" >> "$LOG"
    exit 42
  fi
  echo "PROBE_OK $(date -Is)" >> "$LOG"
fi

BATCH_LOG_OFFSET=$(wc -c < "$LOG" | tr -d ' ')
if [ ${#CUSTOM_CMD[@]} -gt 0 ]; then
  "${CUSTOM_CMD[@]}" >> "$LOG" 2>&1 &
else
  PROMPT_TEXT=$(cat "$PROMPT_FILE")
  "$OPENCODE_BIN" run --model "$MODEL" "$PROMPT_TEXT" >> "$LOG" 2>&1 &
fi
OCPID=$!
echo "BATCH_STARTED pid=$OCPID $(date -Is)" >> "$LOG"

END_REASON=""
EXIT_CODE=""

while kill -0 "$OCPID" 2>/dev/null; do
  slept=0
  while [ "$slept" -lt "$POLL_INTERVAL" ] && kill -0 "$OCPID" 2>/dev/null; do
    sleep 1
    slept=$((slept + 1))
  done

  if _has_quota_error "$LOG" "$BATCH_LOG_OFFSET"; then
    echo "WATCHDOG_QUOTA: quota/API error detected in run.log $(date -Is)" >> "$LOG"
    _terminate_child "$OCPID" "$KILL_GRACE"
    END_REASON="quota"
    EXIT_CODE=43
    break
  fi

  if ! kill -0 "$OCPID" 2>/dev/null; then
    break
  fi

  NOW=$(date +%s)
  MT=$(stat -c %Y "$LOG" 2>/dev/null || echo "$NOW")
  if [ $((NOW - MT)) -ge "$IDLE_TIMEOUT" ]; then
    echo "WATCHDOG_KILL: run.log silent over ${IDLE_TIMEOUT}s $(date -Is)" >> "$LOG"
    _terminate_child "$OCPID" "$KILL_GRACE"
    END_REASON="idle_timeout"
    EXIT_CODE=124
    break
  fi
done

wait "$OCPID" 2>/dev/null
RAW_EXIT=$?

if [ -z "$END_REASON" ]; then
  if _has_quota_error "$LOG" "$BATCH_LOG_OFFSET"; then
    END_REASON="quota"
    EXIT_CODE=43
  elif [ "$RAW_EXIT" -eq 0 ]; then
    END_REASON="ok"
    EXIT_CODE=0
  else
    END_REASON="cmd_fail"
    EXIT_CODE="$RAW_EXIT"
  fi
fi

echo "END_REASON:$END_REASON" >> "$LOG"
echo "EXIT:$EXIT_CODE" >> "$LOG"
echo "=== run_batch.sh end $(date -Is) ===" >> "$LOG"
exit "$EXIT_CODE"
