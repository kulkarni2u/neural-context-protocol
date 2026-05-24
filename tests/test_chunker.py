import json

from ncp.chunker import chunk_code, chunk_content, chunk_json, chunk_prose, chunk_table, detect_type


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
