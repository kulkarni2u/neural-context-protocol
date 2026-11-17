#!/usr/bin/env bash
# Claude Code lifecycle hooks for NCP: SessionStart(compact|clear), SubagentStart,
# SubagentStop and Stop. Usage: ncp-lifecycle-hook.sh <session-restart|subagent-start|subagent-stop|stop>
#
# - session-restart / subagent-start: fetch an NCP context block and inject it as
#   hookSpecificOutput.additionalContext (factual project context).
# - subagent-stop / stop: post the finished turn to NCP (ncp_post_turn) out of band;
#   nothing is written to stdout.
#
# Never fails the host session: every error path exits 0, and nothing happens when
# the bus is down. No autostart (the SessionStart hook owns that).
#
# Env (optional): NCP_HOST (127.0.0.1), NCP_PORT (4242), NCP_TASK, NCP_SLOT,
# NCP_AGENT_ID (claude; session agent id), NCP_HOOKS_DISABLE=1,
# NCP_HOOK_<EVENT>_DISABLE=1 with EVENT in SESSIONSTART|SUBAGENTSTART|SUBAGENTSTOP|STOP,
# NCP_HOOK_POST_FULL=1 (also post result_full; default posts only result_summary).
# Every call carries a session_id (<claude session>[:<subagent agent_id>]); posted
# turns set redact:true so the server redacts secrets before they enter shared memory.
set -uo pipefail
trap 'exit 0' EXIT

EMPTY_OBJ='{}'
MODE="${1:-}"
case "$MODE" in
  session-restart) EVENT=SESSIONSTART; HOOK_NAME=SessionStart ;;
  subagent-start)  EVENT=SUBAGENTSTART; HOOK_NAME=SubagentStart ;;
  subagent-stop)   EVENT=SUBAGENTSTOP; HOOK_NAME=SubagentStop ;;
  stop)            EVENT=STOP; HOOK_NAME=Stop ;;
  *) exit 0 ;;
esac

[ "${NCP_HOOKS_DISABLE:-0}" = "1" ] && exit 0
eval "[ \"\${NCP_HOOK_${EVENT}_DISABLE:-0}\" = \"1\" ]" && exit 0

HOST="${NCP_HOST:-127.0.0.1}"
PORT="${NCP_PORT:-4242}"
BASE="http://${HOST}:${PORT}"

HAVE_JQ=0
command -v jq >/dev/null 2>&1 && HAVE_JQ=1
if [ "$HAVE_JQ" = "0" ] && ! command -v python3 >/dev/null 2>&1; then exit 0; fi

PAYLOAD="$(cat 2>/dev/null || true)"

# jget <key>: top-level string field of the stdin payload ("" when absent).
jget() {
  if [ "$HAVE_JQ" = "1" ]; then
    printf '%s' "$PAYLOAD" | jq -r --arg k "$1" '.[$k] // "" | if type == "string" then . else tostring end' 2>/dev/null
  else
    printf '%s' "$PAYLOAD" | python3 -c 'import json,sys
try:
    v = json.load(sys.stdin).get(sys.argv[1], "")
except Exception:
    v = ""
print(v if isinstance(v, str) else ("" if v is None else json.dumps(v)))' "$1" 2>/dev/null
  fi
}

# slug <text> <fallback>: whitespace-free snake_case identifier.
slug() {
  local s
  s="$(printf '%s' "$1" | tr -c 'A-Za-z0-9_\n' '_' | tr -s '_' | cut -c1-48)"
  s="${s#_}"; s="${s%_}"
  printf '%s' "${s:-$2}"
}

# build_args <agent_id> <role> <task> <slot> <intent> [extra_json_object]: JSON arguments.
build_args() {
  if [ "$HAVE_JQ" = "1" ]; then
    jq -cn --arg a "$1" --arg r "$2" --arg t "$3" --arg s "$4" --arg i "$5" \
      --argjson x "${6:-$EMPTY_OBJ}" --arg sid "${NCP_SID:-}" \
      '{agent_id:$a,role:$r,task:$t,slot:$s,intent:$i} + (if $sid == "" then {} else {session_id:$sid} end) + $x'
  else
    python3 -c 'import json,sys
a=sys.argv
d=dict(agent_id=a[1],role=a[2],task=a[3],slot=a[4],intent=a[5])
if a[7]:
    d["session_id"]=a[7]
d.update(json.loads(a[6]))
print(json.dumps(d))' \
      "$1" "$2" "$3" "$4" "$5" "${6:-$EMPTY_OBJ}" "${NCP_SID:-}"
  fi
}

# rpc <tool> <arguments_json>: JSON-RPC tools/call body on stdout.
rpc_body() {
  if [ "$HAVE_JQ" = "1" ]; then
    jq -cn --arg n "$1" --argjson a "$2" \
      '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:$n,arguments:$a}}'
  else
    python3 -c 'import json,sys
print(json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":sys.argv[1],"arguments":json.loads(sys.argv[2])}}))' "$1" "$2"
  fi
}

call() { # call <tool> <arguments_json>
  rpc_body "$1" "$2" | curl -fsS --max-time 3 -H 'Content-Type: application/json' \
    --data-binary @- "${BASE}/mcp" 2>/dev/null
}

# context_of: extract the "context" string from a JSON-RPC response on stdin.
context_of() {
  if [ "$HAVE_JQ" = "1" ]; then
    jq -r '.result.content[0].text | fromjson | .context // ""' 2>/dev/null
  else
    python3 -c 'import json,sys
try:
    print(json.loads(json.load(sys.stdin)["result"]["content"][0]["text"]).get("context",""))
except Exception:
    pass' 2>/dev/null
  fi
}

emit() { # emit <context> <cap>
  local ctx="${1:0:$2}"
  [ -n "$ctx" ] || return 0
  if [ "$HAVE_JQ" = "1" ]; then
    jq -cn --arg e "$HOOK_NAME" --arg c "$ctx" \
      '{hookSpecificOutput:{hookEventName:$e,additionalContext:$c}}'
  else
    python3 -c 'import json,sys
print(json.dumps({"hookSpecificOutput":{"hookEventName":sys.argv[1],"additionalContext":sys.argv[2]}}))' "$HOOK_NAME" "$ctx"
  fi
}

SESSION_ID="$(jget session_id)"
SHORT="$(slug "${SESSION_ID:0:8}" session)"
SESSION_AGENT="$(slug "${NCP_AGENT_ID:-claude}" claude)"
NCP_SID="$SESSION_ID"

case "$MODE" in
  session-restart|stop)
    AGENT_ID="$SESSION_AGENT"; ROLE="$SESSION_AGENT"
    TASK="${NCP_TASK:-session_${SHORT}}"; SLOT="${NCP_SLOT:-session}" ;;
  *)
    AGENT_ID="$(slug "$(jget agent_type)" subagent)"; ROLE="$AGENT_ID"
    # Per-subagent session so concurrent subagents never share server-side state.
    SUB_ID="$(jget agent_id)"; [ -n "$SUB_ID" ] || SUB_ID="$AGENT_ID"
    [ -n "$SESSION_ID" ] && NCP_SID="${SESSION_ID}:${SUB_ID}"
    TASK="${NCP_TASK:-session_${SHORT}}"; SLOT="${NCP_SLOT:-$AGENT_ID}" ;;
esac
TASK="$(slug "$TASK" session)"; SLOT="$(slug "$SLOT" session)"

# Skip entirely when the bus is down.
curl -fsS -o /dev/null --max-time 3 "${BASE}/healthz" 2>/dev/null || exit 0

case "$MODE" in
  session-restart)
    ARGS="$(build_args "$AGENT_ID" "$ROLE" "$TASK" "$SLOT" session_restart '{"delta":false}')"
    emit "$(call ncp_get_context "$ARGS" | context_of)" 6000 ;;
  subagent-start)
    ARGS="$(build_args "$AGENT_ID" "$ROLE" "$TASK" "$SLOT" subagent_start)"
    emit "$(call ncp_get_context "$ARGS" | context_of)" 4000 ;;
  subagent-stop|stop)
    MSG="$(jget last_assistant_message)"
    [ -n "$MSG" ] || exit 0
    INTENT=session_stop; [ "$MODE" = "subagent-stop" ] && INTENT=subagent_stop
    # Truncate by characters inside jq/python so a multi-byte character is never split.
    POST_FULL=0; [ "${NCP_HOOK_POST_FULL:-0}" = "1" ] && POST_FULL=1
    if [ "$HAVE_JQ" = "1" ]; then
      EXTRA="$(jq -cn --arg m "$MSG" --argjson full "$POST_FULL" \
        '{result_summary:$m[0:400],redact:true} + (if $full == 1 then {result_full:$m[0:8000]} else {} end)')"
    else
      EXTRA="$(python3 -c 'import json,sys
m=sys.argv[1]
d={"result_summary":m[:400],"redact":True}
if sys.argv[2]=="1":
    d["result_full"]=m[:8000]
print(json.dumps(d))' "$MSG" "$POST_FULL")"
    fi
    ARGS="$(build_args "$AGENT_ID" "$ROLE" "$TASK" "$SLOT" "$INTENT" "$EXTRA")"
    call ncp_post_turn "$ARGS" >/dev/null || true ;;
esac
exit 0
