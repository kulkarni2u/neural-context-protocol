"""Claude Code lifecycle hooks (SessionStart compact|clear, SubagentStart/Stop, Stop)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SCRIPT = REPO_ROOT / "claude-plugin" / "hooks" / "scripts" / "ncp-lifecycle-hook.sh"
TEMPLATE_DIR = REPO_ROOT / "ncp" / "templates" / "provider_hooks" / "claude"
TEMPLATE_SCRIPT = TEMPLATE_DIR / "hooks" / "ncp-lifecycle-hook.sh"
PLUGIN_HOOKS = REPO_ROOT / "claude-plugin" / "hooks" / "hooks.json"
TEMPLATE_SETTINGS = TEMPLATE_DIR / "settings.json"

MODES = {
    "session-restart": "SessionStart",
    "subagent-start": "SubagentStart",
    "subagent-stop": "SubagentStop",
    "stop": "Stop",
}


class _Bus:
    def __init__(self, context: str = "CTX") -> None:
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def do_GET(self):  # healthz
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append(json.loads(body))
                inner = json.dumps({"context": context})
                out = json.dumps({"jsonrpc": "2.0", "id": 1,
                                  "result": {"content": [{"type": "text", "text": inner}]}})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out.encode())

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def bus():
    b = _Bus("C" * 9000)
    yield b
    b.close()


def _run(mode, stdin, port, extra_env=None, path=None):
    env = os.environ.copy()
    for k in list(env):
        if k.startswith("NCP_"):
            del env[k]
    env.update({"NCP_PORT": str(port)})
    env.update(extra_env or {})
    if path is not None:
        env["PATH"] = path
    return subprocess.run(["bash", str(PLUGIN_SCRIPT), mode], input=json.dumps(stdin),
                          env=env, capture_output=True, text=True, timeout=30)


def _args(bus):
    assert len(bus.requests) == 1
    req = bus.requests[0]
    assert req["method"] == "tools/call"
    return req["params"]["name"], req["params"]["arguments"]


def test_plugin_and_template_copies_identical_and_registered():
    assert PLUGIN_SCRIPT.read_bytes() == TEMPLATE_SCRIPT.read_bytes()
    assert os.access(PLUGIN_SCRIPT, os.X_OK) and os.access(TEMPLATE_SCRIPT, os.X_OK)
    plugin = json.loads(PLUGIN_HOOKS.read_text())["hooks"]
    template = json.loads(TEMPLATE_SETTINGS.read_text())["hooks"]
    for cfg in (plugin, template):
        for mode, event in MODES.items():
            cmds = [h["command"] for e in cfg[event] for h in e["hooks"]]
            assert any(c.endswith(f"ncp-lifecycle-hook.sh\" {mode}") or c.endswith(f"ncp-lifecycle-hook.sh {mode}")
                       for c in cmds), (event, cmds)
        restart = [e for e in cfg["SessionStart"] if e.get("matcher") == "compact|clear"]
        assert len(restart) == 1


def test_session_restart_injects_capped_context(bus):
    r = _run("session-restart", {"session_id": "abcdef123456"}, bus.port)
    assert r.returncode == 0
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart"
    assert out["additionalContext"] == "C" * 6000
    name, args = _args(bus)
    assert name == "ncp_get_context"
    assert args == {"agent_id": "claude", "role": "claude", "task": "session_abcdef12",
                    "slot": "session", "intent": "session_restart", "delta": False,
                    "session_id": "abcdef123456"}


def test_subagent_start_derivation_and_cap(bus):
    r = _run("subagent-start", {"session_id": "abcdef123456", "agent_type": "pravaha"}, bus.port)
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "SubagentStart"
    assert out["additionalContext"] == "C" * 4000
    _, args = _args(bus)
    assert args == {"agent_id": "pravaha", "role": "pravaha", "task": "session_abcdef12",
                    "slot": "pravaha", "intent": "subagent_start",
                    "session_id": "abcdef123456:pravaha"}


def test_subagent_start_env_task_slot(bus):
    _run("subagent-start", {"session_id": "s", "agent_type": "vichara"}, bus.port,
         {"NCP_TASK": "my_task", "NCP_SLOT": "my_slot"})
    _, args = _args(bus)
    assert (args["task"], args["slot"]) == ("my_task", "my_slot")


@pytest.mark.parametrize("mode,agent,intent", [("subagent-stop", "nirnaya", "subagent_stop"),
                                               ("stop", "claude", "session_stop")])
def test_stop_posts_turn_silently(bus, mode, agent, intent):
    msg = "m" * 9000
    r = _run(mode, {"session_id": "abcdef123456", "agent_type": "nirnaya",
                    "last_assistant_message": msg}, bus.port)
    assert r.returncode == 0 and r.stdout == ""
    name, args = _args(bus)
    assert name == "ncp_post_turn"
    assert args["agent_id"] == agent and args["intent"] == intent
    assert args["result_summary"] == "m" * 400
    assert args["redact"] is True
    assert "result_full" not in args
    assert args["session_id"] == ("abcdef123456:nirnaya" if mode == "subagent-stop" else "abcdef123456")


def test_stop_posts_full_only_when_opted_in(bus):
    msg = "m" * 9000
    _run("stop", {"session_id": "abcdef123456", "last_assistant_message": msg}, bus.port,
         {"NCP_HOOK_POST_FULL": "1"})
    _, args = _args(bus)
    assert args["result_full"] == "m" * 8000 and args["redact"] is True


def test_subagent_stop_session_uses_stdin_agent_id(bus):
    _run("subagent-stop", {"session_id": "sess1", "agent_type": "nirnaya", "agent_id": "agent-42",
                           "last_assistant_message": "done"}, bus.port)
    _, args = _args(bus)
    assert args["session_id"] == "sess1:agent-42"


@pytest.mark.parametrize("no_jq", [False, True])
def test_truncation_never_splits_multibyte_characters(bus, tmp_path, no_jq):
    path = None
    if no_jq:
        for tool in ("bash", "python3", "curl", "cat", "tr", "cut"):
            (tmp_path / tool).symlink_to(shutil.which(tool))
        path = str(tmp_path)
    elif shutil.which("jq") is None:
        pytest.skip("jq not installed")
    msg = "\u00e9\u4e2d" * 5000
    _run("stop", {"session_id": "s1", "last_assistant_message": msg}, bus.port,
         {"NCP_HOOK_POST_FULL": "1", "LC_ALL": "C"}, path=path)
    _, args = _args(bus)
    assert args["result_summary"] == msg[:400]
    assert args["result_full"] == msg[:8000]


def test_stop_skips_empty_message(bus):
    r = _run("stop", {"session_id": "x", "last_assistant_message": ""}, bus.port)
    assert r.returncode == 0 and r.stdout == ""
    assert bus.requests == []


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_python_fallback_without_jq(bus, tmp_path):
    for tool in ("bash", "python3", "curl", "cat", "tr", "cut"):
        (tmp_path / tool).symlink_to(shutil.which(tool))
    r = _run("subagent-start", {"session_id": "abcdef123456", "agent_type": "pravaha"},
             bus.port, path=str(tmp_path))
    assert json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] == "C" * 4000
    _, args = _args(bus)
    assert args["agent_id"] == "pravaha"


@pytest.mark.parametrize("mode", list(MODES))
def test_silent_when_bus_down(mode):
    r = _run(mode, {"session_id": "s", "agent_type": "a", "last_assistant_message": "x"}, 9)
    assert r.returncode == 0 and r.stdout == ""


@pytest.mark.parametrize("mode,event", [(m, e) for m, e in MODES.items()])
@pytest.mark.parametrize("flag", ["NCP_HOOKS_DISABLE", None])
def test_disabled(bus, mode, event, flag):
    flag = flag or f"NCP_HOOK_{event.upper()}_DISABLE"
    r = _run(mode, {"session_id": "s", "agent_type": "a", "last_assistant_message": "x"},
             bus.port, {flag: "1"})
    assert r.returncode == 0 and r.stdout == ""
    assert bus.requests == []
