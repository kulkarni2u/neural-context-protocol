"""`ncp skill cache|list|show` and `--skill` dispatch on handoff / run-turn."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from ncp.cli import main
from ncp.skill_cache import cache_skill
from ncp.stores.sqlite import SQLiteStore
from ncp.types import Whisper

PIPE = "pipe_skill_cli"
WORKFLOW = "# Deploy\n\n1. Run tests.\n2. Tag release.  \n\n\tIndented <b>raw</b> text sk-abc123def456ghi789\n"
CHECKLIST = "# Review\n\n- check types\n- check tests\n"


def _init(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["init", "--cwd", str(tmp_path)])
    assert result.exit_code == 0, result.output


def _file(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def _cache(tmp_path: Path, path: Path, *extra: str):
    return CliRunner().invoke(main, ["skill", "cache", str(path), "--cwd", str(tmp_path), *extra])


def _register(tmp_path: Path, skill_id: str, text: str, *extra: str) -> dict:
    if "--pipeline" not in extra and "--global" not in extra:
        extra = (*extra, "--global")
    result = _cache(tmp_path, _file(tmp_path, "s.md", text), "--id", skill_id, "--json", *extra)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_cache_requires_explicit_scope(tmp_path: Path) -> None:
    _init(tmp_path)
    result = _cache(tmp_path, _file(tmp_path, "a.md", CHECKLIST), "--id", "team/review")
    assert result.exit_code == 2
    assert "--pipeline <id> or --global" in result.output


def test_cache_global_and_pipeline_are_exclusive(tmp_path: Path) -> None:
    _init(tmp_path)
    result = _cache(tmp_path, _file(tmp_path, "a.md", CHECKLIST), "--id", "team/review", "--pipeline", PIPE, "--global")
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_cache_global_scope_reported(tmp_path: Path) -> None:
    _init(tmp_path)
    result = _cache(tmp_path, _file(tmp_path, "a.md", CHECKLIST), "--id", "team/review", "--global")
    assert result.exit_code == 0, result.output
    assert "scope: global" in result.output
    assert "cache_hit: False" in result.output
    assert "kind: reference" in result.output


def test_cache_json_cache_hit_and_kind(tmp_path: Path) -> None:
    _init(tmp_path)
    first = _register(tmp_path, "team/deploy", WORKFLOW, "--kind", "workflow")
    second = _register(tmp_path, "team/deploy", WORKFLOW, "--kind", "workflow")
    assert first["kind"] == "workflow"
    assert first["cache_hit"] is False and second["cache_hit"] is True
    assert first["content_hash"] == second["content_hash"]
    assert first["sections"] >= 1 and first["scope"] == "global"


def test_pipeline_scope_and_global_flag(tmp_path: Path) -> None:
    _init(tmp_path)
    scoped = _register(tmp_path, "team/scoped", CHECKLIST, "--pipeline", PIPE)
    assert scoped["scope"] == "pipeline" and scoped["pipeline_id"] == PIPE
    forced = _register(tmp_path, "team/forced", CHECKLIST, "--global")
    assert forced["scope"] == "global"

    def ids(*args: str) -> set[str]:
        out = CliRunner().invoke(main, ["skill", "list", "--cwd", str(tmp_path), "--json", *args])
        assert out.exit_code == 0, out.output
        return {row["skill_id"] for row in json.loads(out.stdout)}

    assert ids() == {"team/forced"}
    assert ids("--pipeline", PIPE) == {"team/forced", "team/scoped"}
    assert ids("--pipeline", "other") == {"team/forced"}


def test_invalid_skill_id_rejected(tmp_path: Path) -> None:
    _init(tmp_path)
    result = _cache(tmp_path, _file(tmp_path, "a.md", CHECKLIST), "--id", "no-namespace", "--global")
    assert result.exit_code != 0
    assert "skill_id" in result.output


def test_invalid_kind_rejected(tmp_path: Path) -> None:
    _init(tmp_path)
    result = _cache(tmp_path, _file(tmp_path, "a.md", CHECKLIST), "--id", "t/x", "--kind", "bogus", "--global")
    assert result.exit_code != 0


def test_hint_when_skill_cache_disabled(tmp_path: Path) -> None:
    _init(tmp_path)
    result = _cache(tmp_path, _file(tmp_path, "a.md", CHECKLIST), "--id", "t/x", "--global")
    assert "skill_cache" in result.output and "enabled" in result.output


def test_list_and_show_verbatim(tmp_path: Path) -> None:
    _init(tmp_path)
    info = _register(tmp_path, "team/deploy", WORKFLOW, "--kind", "workflow")
    listed = CliRunner().invoke(main, ["skill", "list", "--cwd", str(tmp_path)])
    assert "team/deploy@" + info["content_hash"][:12] in listed.output
    assert "kind:workflow" in listed.output
    shown = CliRunner().invoke(main, ["skill", "show", "team/deploy", "--cwd", str(tmp_path)])
    assert shown.output == WORKFLOW.strip()
    by_hash = CliRunner().invoke(
        main, ["skill", "show", "team/deploy", "--hash", info["content_hash"], "--cwd", str(tmp_path)]
    )
    assert by_hash.output == shown.output
    missing = CliRunner().invoke(main, ["skill", "show", "team/none", "--cwd", str(tmp_path)])
    assert missing.exit_code != 0


def _capture_subprocess(monkeypatch: pytest.MonkeyPatch, stdout: str) -> list[str]:
    prompts: list[str] = []

    def fake(*, runner_name, command, cwd, prompt, timeout_seconds, env=None):
        prompts.append(prompt)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr("ncp.agent_handoff._run_handoff_subprocess", fake)
    return prompts


def _emit_whisper(tmp_path: Path) -> None:
    SQLiteStore(tmp_path / ".ncp" / "store.db").emit_whisper(
        Whisper(
            from_agent="codex",
            target="claude",
            whisper_type="nudge",
            payload="do the thing",
            confidence=0.95,
            pipeline_id=PIPE,
        )
    )


def _handoff(tmp_path: Path, *skills: str):
    args = ["handoff", "claude", "--cwd", str(tmp_path), "--pipeline-id", PIPE]
    for skill in skills:
        args += ["--skill", skill]
    return CliRunner().invoke(main, args)


def test_handoff_skill_prefix_order_and_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init(tmp_path)
    a = _register(tmp_path, "team/deploy", WORKFLOW, "--kind", "workflow")
    b = _register(tmp_path, "team/review", CHECKLIST)
    _emit_whisper(tmp_path)
    prompts = _capture_subprocess(monkeypatch, "done")
    result = _handoff(tmp_path, "team/review", "team/deploy")
    assert result.exit_code == 0, result.output
    prompt = prompts[0]
    first = f"[NCP:SKILL] skill:team/review@{b['content_hash'][:12]} kind:reference\n{CHECKLIST.strip()}"
    second = f"[NCP:SKILL] skill:team/deploy@{a['content_hash'][:12]} kind:workflow\n{WORKFLOW.strip()}"
    assert prompt.startswith(first + "\n\n" + second)
    assert "user-authored procedures" in prompt
    assert prompt.index("[NCP:SKILL]") < prompt.index("Repository root:")
    assert "sk-abc123def456ghi789" in prompt  # not redacted


def test_handoff_rejects_unregistered_skill_before_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path)
    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    cache_skill("agent text", skill_id="evil/skill", store=store)  # no registered_by
    _emit_whisper(tmp_path)
    prompts = _capture_subprocess(monkeypatch, "done")
    result = _handoff(tmp_path, "evil/skill")
    assert result.exit_code != 0
    assert "not user-registered" in result.output
    assert prompts == []
    missing = _handoff(tmp_path, "team/missing")
    assert missing.exit_code != 0 and "not cached" in missing.output
    assert prompts == []


def test_handoff_uses_registered_version_when_newer_is_unregistered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path)
    info = _register(tmp_path, "team/review", CHECKLIST, "--pipeline", PIPE)
    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    cache_skill("newer unregistered text", skill_id="team/review", store=store, pipeline_id=PIPE)
    _emit_whisper(tmp_path)
    prompts = _capture_subprocess(monkeypatch, "done")
    result = _handoff(tmp_path, "team/review")
    assert result.exit_code == 0, result.output
    assert f"skill:team/review@{info['content_hash'][:12]}" in prompts[0]
    assert "newer unregistered text" not in prompts[0]


def test_handoff_resolves_global_registered_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init(tmp_path)
    info = _register(tmp_path, "team/review", CHECKLIST)
    _emit_whisper(tmp_path)
    prompts = _capture_subprocess(monkeypatch, "done")
    result = _handoff(tmp_path, "team/review")
    assert result.exit_code == 0, result.output
    assert f"skill:team/review@{info['content_hash'][:12]}" in prompts[0]


def test_skill_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init(tmp_path)
    _register(tmp_path, "team/big", ("lorem ipsum " * 6000))
    _emit_whisper(tmp_path)
    prompts = _capture_subprocess(monkeypatch, "done")
    result = _handoff(tmp_path, "team/big")
    assert result.exit_code != 0
    assert "60000" in result.output
    assert prompts == []


def test_run_turn_skill_prefix_matches_handoff_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init(tmp_path)
    info = _register(tmp_path, "team/deploy", WORKFLOW, "--kind", "workflow")
    prompts: list[str] = []

    def fake_provider(*, host, prompt, workspace, timeout_seconds):
        prompts.append(prompt)
        return "ok"

    monkeypatch.setattr("ncp.agent_handoff.run_provider_turn", fake_provider)
    result = CliRunner().invoke(
        main,
        ["run-turn", "--cwd", str(tmp_path), "--agent", "w1", "--pipeline", PIPE, "--host", "mock",
         "--task", "t", "--slot", "s", "--prompt", "go", "--skill", "team/deploy"],
    )
    assert result.exit_code == 0, result.output
    expected = f"[NCP:SKILL] skill:team/deploy@{info['content_hash'][:12]} kind:workflow\n{WORKFLOW.strip()}"
    assert prompts[0].startswith(expected + "\n\n")
    assert prompts[0].index("user-authored procedures") < prompts[0].index("Bounded NCP context:")
    assert prompts[0].endswith("go")


def test_run_turn_rejects_unregistered_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init(tmp_path)
    cache_skill("agent text", skill_id="evil/skill", store=SQLiteStore(tmp_path / ".ncp" / "store.db"))
    monkeypatch.setattr(
        "ncp.agent_handoff.run_provider_turn", lambda **_: pytest.fail("provider must not run")
    )
    result = CliRunner().invoke(
        main,
        ["run-turn", "--cwd", str(tmp_path), "--agent", "w1", "--pipeline", PIPE, "--host", "mock",
         "--task", "t", "--slot", "s", "--prompt", "go", "--skill", "evil/skill"],
    )
    assert result.exit_code != 0
    assert "not user-registered" in result.output
