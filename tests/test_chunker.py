import json

from ncp.chunker import (
    chunk_code,
    chunk_content,
    chunk_json,
    chunk_prose,
    chunk_table,
    collapse_blank_lines,
    dedup_consecutive_lines,
    detect_type,
    filter_content,
    filter_json_noise,
    strip_ansi,
    strip_boilerplate,
)


def test_detect_type_auto_paths() -> None:
    assert detect_type('{"key": "value"}') == "json"
    assert detect_type("def run():\n    return True") == "code"
    assert detect_type("| a | b |\n| - | - |\n| 1 | 2 |") == "table"
    assert detect_type("This is a plain sentence.") == "prose"


def test_chunk_prose_respects_sentence_boundaries() -> None:
    content = "One short sentence. Two more words here. Final sentence closes."

    chunks = chunk_prose(content, max_tokens=5)

    assert chunks == [
        "One short sentence.",
        "Two more words here.",
        "Final sentence closes.",
    ]


def test_chunk_json_splits_top_level_and_recurses_one_level() -> None:
    payload = json.dumps(
        {
            "summary": "ok",
            "details": {
                "alpha": " ".join(["a"] * 120),
                "beta": " ".join(["b"] * 120),
            },
        }
    )

    chunks = chunk_json(payload, max_tokens=200)

    assert '{"summary":"ok"}' in chunks
    assert any('"details":{"alpha":' in chunk for chunk in chunks)
    assert any('"details":{"beta":' in chunk for chunk in chunks)


def test_chunk_code_splits_on_function_boundaries() -> None:
    content = "\n".join(
        [
            "import os",
            "",
            "def first():",
            "    return 1",
            "",
            "def second():",
            "    return 2",
        ]
    )

    chunks = chunk_code(content, max_tokens=50)

    assert chunks == [
        "import os",
        "def first():\n    return 1",
        "def second():\n    return 2",
    ]


def test_chunk_code_falls_back_to_line_windows_without_boundaries() -> None:
    content = "\n".join(f"line_{index}" for index in range(65))

    chunks = chunk_code(content, max_tokens=1000, fallback_lines=30)

    assert len(chunks) == 3
    assert chunks[0].splitlines()[0] == "line_0"
    assert chunks[1].splitlines()[0] == "line_30"
    assert chunks[2].splitlines()[0] == "line_60"


def test_chunk_table_preserves_header_per_group() -> None:
    rows = "\n".join(f"| {index} | value_{index} |" for index in range(1, 8))
    table = "| id | value |\n| -- | ----- |\n" + rows

    chunks = chunk_table(table)

    assert len(chunks) == 2
    assert chunks[0].splitlines()[0] == "| id | value |"
    assert chunks[1].splitlines()[0] == "| id | value |"
    assert len(chunks[0].splitlines()) == 7
    assert len(chunks[1].splitlines()) == 4


def test_chunk_content_auto_dispatches_large_structured_payload() -> None:
    payload = json.dumps(
        {
            "meta": {"status": "ok"},
            "items": [{"id": index, "value": f"value_{index}"} for index in range(12)],
        }
    )

    chunks = chunk_content(payload, chunk_type="auto", max_tokens=40)

    assert len(chunks) <= 20
    assert all(chunk.strip() for chunk in chunks)


# ---------------------------------------------------------------------------
# Ingestion-time filtering tests
# ---------------------------------------------------------------------------


def test_strip_ansi_removes_escape_sequences() -> None:
    raw = "\x1b[32mPASSED\x1b[0m test_foo.py::test_bar"
    assert strip_ansi(raw) == "PASSED test_foo.py::test_bar"


def test_strip_ansi_noop_on_clean_text() -> None:
    clean = "no colors here"
    assert strip_ansi(clean) == clean


def test_collapse_blank_lines_reduces_runs() -> None:
    text = "line1\n\n\n\n\nline2\n\n\nline3"
    result = collapse_blank_lines(text)
    assert result == "line1\n\nline2\n\nline3"


def test_collapse_blank_lines_preserves_single_blanks() -> None:
    text = "a\n\nb"
    assert collapse_blank_lines(text) == "a\n\nb"


def test_dedup_consecutive_lines_collapses_repeats() -> None:
    text = "ok\nok\nok\nok\ndone"
    result = dedup_consecutive_lines(text)
    assert result == "ok  (×4)\ndone"


def test_dedup_consecutive_lines_preserves_unique() -> None:
    text = "a\nb\nc"
    assert dedup_consecutive_lines(text) == text


def test_strip_boilerplate_removes_progress_bars() -> None:
    text = "  45% |████        |\nBuilding...\n100% |████████████|"
    result = strip_boilerplate(text)
    assert "45%" not in result
    assert "Building..." in result


def test_strip_boilerplate_removes_timing_lines() -> None:
    text = "real\t0m1.234s\nuser\t0m0.890s\nsys\t0m0.120s\nDone."
    result = strip_boilerplate(text)
    assert "real" not in result
    assert "Done." in result


def test_filter_json_noise_prunes_null_and_empty() -> None:
    raw = json.dumps({"key": "value", "empty": "", "null_val": None, "list": []})
    result = filter_json_noise(raw)
    parsed = json.loads(result)
    assert parsed == {"key": "value"}


def test_filter_json_noise_passthrough_non_json() -> None:
    text = "not json at all"
    assert filter_json_noise(text) == text


def test_filter_content_combines_all_filters() -> None:
    raw = "\x1b[31mERROR\x1b[0m: fail\nERROR: fail\nERROR: fail\n\n\n\n\nresult: ok"
    result = filter_content(raw, content_type="prose")
    assert result.was_filtered
    assert "\x1b[" not in result.filtered
    assert "(×3)" in result.filtered
    assert result.reduction_ratio > 0.0


def test_filter_content_no_change_for_clean_input() -> None:
    clean = "NPE at PaymentProcessor.java:142. root_cause: retryCount is null."
    result = filter_content(clean)
    assert not result.was_filtered
    assert result.filtered == clean
    assert result.reduction_ratio == 0.0


def test_filter_content_json_prunes_nulls() -> None:
    raw = json.dumps({"status": "ok", "error": None, "warnings": []})
    result = filter_content(raw, content_type="json")
    assert result.was_filtered
    parsed = json.loads(result.filtered)
    assert "error" not in parsed
    assert "warnings" not in parsed
    assert parsed["status"] == "ok"
