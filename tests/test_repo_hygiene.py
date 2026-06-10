from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dev_up_script_and_makefile_target_are_present() -> None:
    script = ROOT / "scripts" / "dev_up.sh"
    makefile = ROOT / "Makefile"

    assert script.exists()
    assert os.access(script, os.X_OK)
    script_text = script.read_text()
    assert "scripts/infra_up.sh" in script_text
    assert "MigrationRunner" in script_text
    assert "python3 -m pytest" in script_text

    makefile_text = makefile.read_text()
    assert "dev:" in makefile_text
    assert "scripts/dev_up.sh" in makefile_text


def test_pgvector_integration_script_uses_psycopg3_dependency() -> None:
    script_text = (ROOT / "scripts" / "test_pgvector_integration.sh").read_text()

    assert 'find_spec("psycopg")' in script_text
    assert "import psycopg" in script_text
    assert "psycopg2" not in script_text


def test_ci_has_minimum_python_import_and_benchmark_gates() -> None:
    ci_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "minimum-python-import" in ci_text
    assert 'python-version: "3.11"' in ci_text
    assert "python -c \"import ncp" in ci_text
    assert "benchmarks/coding_pipeline/run.py" in ci_text
    assert "benchmarks/needle/run.py" in ci_text


def test_provider_adapter_tests_skip_when_optional_sdks_are_absent() -> None:
    adapter_tests = (ROOT / "tests" / "test_adapters.py").read_text()

    assert 'pytest.importorskip("anthropic"' in adapter_tests
    assert 'pytest.importorskip("openai"' in adapter_tests
    assert 'pytest.importorskip("cohere"' in adapter_tests
    assert 'pytest.importorskip("google.genai"' in adapter_tests
    assert 'pytest.importorskip("mistralai.client"' in adapter_tests
