"""``ncp ontology wiki``: manifest -> markdown wiki, compare, and the checklist contract."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from click.testing import CliRunner

from ncp.cli import main
from ncp.ontology.scan import scan_repository
from ncp.ontology.wiki import build_wiki, slugify

JAVA = "svc/src/main/java/com/acme/K.java"

CHECKLIST_RE = re.compile(r"^\s*[-*]\s*\[[xX ]\]\s*`([^`]+?) -([A-Z][A-Z0-9_]*)-> ([^`]+?)`")


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _scanned(tmp_path: Path) -> tuple[dict, dict]:
    root = _write(tmp_path / "repo", {
        "svc/build.gradle": "plugins { id 'java' }\n",
        "svc/Dockerfile": "FROM x\n",
        JAVA: (
            "class K {\n"
            '  @KafkaListener(topics = "orders.v1") void a(String s) {}\n'
            '  @KafkaListener(topics = "${x.y}") void b(String s) {}\n'
            '  void c() { kafkaTemplate.send("audit.v1", "x"); }\n'
            "}\n"
        ),
    })
    result = scan_repository(root)
    return json.loads(result.to_json()), dict(result.summary)


def _hand() -> dict:
    def ent(t: str, k: str, **attrs: object) -> dict:
        return {"type": t, "key": k, "attributes": attrs}

    def rel(src: str, t: str, dst: str, conf: float = 0.6, status: str = "candidate", **attrs: object) -> dict:
        return {"src": src, "type": t, "dst": dst, "status": status, "confidence": conf,
                "attestation": "a/b.java#L12", "attributes": attrs}

    return {
        "pipeline": "p", "source": "s", "schema_version": 2,
        "entities": [
            ent("Service", "billing", detected_by="k8s"), ent("Service", "shop"),
            ent("Module", "worker", path="worker"),
            ent("Queue", "rabbit:invoices", broker="rabbit", name="invoices"),
            ent("Queue", "rabbit:dead", broker="rabbit", name="dead"),
            ent("Exchange", "rabbit:ex.in", broker="rabbit", name="ex.in"),
            ent("KafkaTopic", "orders.v1", name="orders.v1"),
            ent("KafkaTopic", "orders.v2", name="orders.v2"),
            ent("KafkaTopic", "pattern:orders.*", name="pattern:orders.*", pattern=True),
            ent("KafkaTopic", "unresolved:${t}", name="unresolved:${t}", unresolved=True, spel="#{env.t}"),
        ],
        "relations": [
            rel("Service:billing", "PRODUCES", "Exchange:rabbit:ex.in", resolved_from="literal"),
            rel("Exchange:rabbit:ex.in", "ROUTES_TO", "Queue:rabbit:invoices", status="verified", conf=0.9),
            rel("Module:worker", "CONSUMES", "Queue:rabbit:invoices", via_module="worker"),
            rel("Service:shop", "PRODUCES", "Queue:rabbit:dead"),
            rel("Service:shop", "PRODUCES", "KafkaTopic:orders.v1"),
            rel("Service:billing", "PRODUCES", "KafkaTopic:orders.v2"),
            rel("Service:billing", "CONSUMES", "KafkaTopic:pattern:orders.*"),
            rel("Service:shop", "CONSUMES", "KafkaTopic:unresolved:${t}", conf=0.4),
            rel("Service:shop", "DEPENDS_ON", "Service:billing", status="verified", conf=0.9),
        ],
    }


def _page(pages: dict[str, str], prefix: str) -> str:
    (name,) = [p for p in pages if p.startswith(prefix)]
    return pages[name]


def test_scan_manifest_pages(tmp_path: Path) -> None:
    manifest, summary = _scanned(tmp_path)
    pages = build_wiki(manifest, summary=summary)
    assert set(pages) == {
        "index.md", "messaging.md", "coverage.md", "candidates.md", "services/svc.md",
        "destinations/kafkatopic-orders.v1.md", "destinations/kafkatopic-audit.v1.md",
        "destinations/kafkatopic-unresolved-x.y.md",
    }
    orders = pages["destinations/kafkatopic-orders.v1.md"]
    assert "| Producers | Consumers |" in orders
    assert "[Service:svc](../services/svc.md)" in orders
    assert "⚠ no producer in repo" in orders
    assert "no consumer" not in orders
    assert "⚠ no consumer in repo" in pages["destinations/kafkatopic-audit.v1.md"]
    assert "⚠ unresolved name" in pages["destinations/kafkatopic-unresolved-x.y.md"]
    service = pages["services/svc.md"]
    assert "## Produces" in service and "## Consumes" in service
    assert "svc/src/main/java/com/acme/K.java#L" in service
    coverage = pages["coverage.md"]
    assert "## Per-broker coverage" in coverage and "| kafka |" in coverage
    assert "producer_sites" in coverage
    assert "(destinations/kafkatopic-orders.v1.md)" in pages["index.md"]
    # messaging matrix lists problems first
    matrix = pages["messaging.md"]
    assert matrix.index("audit.v1") < matrix.index("| kafka |") or "audit.v1" in matrix


def test_hand_manifest_generic_rendering() -> None:
    pages = build_wiki(_hand(), summary={
        "messaging": {"rabbit": {"sites": 3, "matched_sites": 2, "unmatched_examples": ["x.java#L1: foo"]}},
        "attribution": {"edges_from_service": 4, "edges_via_dependency": 1, "edges_from_module": 2},
        "warnings": ["careful"],
    })
    assert "modules/worker.md" in pages and "services/billing.md" in pages
    queue = pages["destinations/queue-rabbit-invoices.md"]
    assert "[Module:worker](../modules/worker.md)" in queue
    assert "Exchange:rabbit:ex.in" in queue  # routed from
    assert "no producer in repo" not in queue  # reachable via exchange
    assert "⚠ no consumer in repo" in pages["destinations/queue-rabbit-dead.md"]
    assert "worker" in pages["modules/worker.md"] and "via module" in pages["modules/worker.md"].lower()
    # pattern consumer is listed on the matching topic
    v2 = pages["destinations/kafkatopic-orders.v2.md"]
    assert "via pattern orders.*" in v2
    assert "no consumer" not in v2
    pattern = pages["destinations/kafkatopic-pattern-orders.md"]
    assert "pattern subscription" in pattern
    assert "orders.v1" in pattern and "orders.v2" in pattern
    coverage = pages["coverage.md"]
    assert "rabbit" in coverage and "edges_via_dependency" in coverage and "x.java#L1: foo" in coverage
    assert "#{env.t}" in coverage and "orders.\\*" not in coverage
    assert "careful" in pages["index.md"]
    assert "Depended on by" in pages["services/billing.md"]


def test_checklist_contract() -> None:
    manifest = _hand()
    pages = build_wiki(manifest)
    text = pages["candidates.md"]
    assert "ncp ontology promote --from-checklist candidates.md" in text
    lines = [ln for ln in text.splitlines() if ln.lstrip().startswith("- [")]
    candidates = [r for r in manifest["relations"] if r["status"] == "candidate"]
    assert len(lines) == len(candidates)
    parsed = set()
    for line in lines:
        match = CHECKLIST_RE.match(line)
        assert match, line
        parsed.add(match.groups())
        assert "conf 0.60" in line or "conf 0.40" in line
        assert "`a/b.java#L12`" in line
    assert parsed == {(r["src"], r["type"], r["dst"]) for r in candidates}
    unresolved = [ln for ln in lines if "unresolved:${t}" in ln]
    assert len(unresolved) == 1 and unresolved[0].endswith("· unresolved")
    ticked = CHECKLIST_RE.match(lines[0].replace("[ ]", "[x]"))
    assert ticked


def test_cells_escape_pipes_and_backticks() -> None:
    manifest = _hand()
    manifest["relations"][3]["attestation"] = "weird|path`x#L1"
    pages = build_wiki(manifest)
    assert "weird\\|path\\`x#L1" in pages["services/shop.md"]


def test_slug_collisions() -> None:
    assert slugify("Foo/Bar Baz") == "foo-bar-baz"
    manifest = {
        "entities": [{"type": "Service", "key": "A B"}, {"type": "Service", "key": "a-b"},
                     {"type": "Service", "key": "a/b"}],
        "relations": [],
    }
    pages = build_wiki(manifest)
    assert {p for p in pages if p.startswith("services/")} == {
        "services/a-b.md", "services/a-b-2.md", "services/a-b-3.md"}


def test_compare_page() -> None:
    old = _hand()
    new = copy.deepcopy(old)
    new["entities"].append({"type": "Service", "key": "extra"})
    new["entities"] = [e for e in new["entities"] if e["key"] != "orders.v2"]
    new["relations"] = [r for r in new["relations"] if r["dst"] != "KafkaTopic:orders.v2"]
    new["relations"].append({"src": "Service:extra", "type": "CONSUMES", "dst": "KafkaTopic:orders.v1",
                             "status": "candidate", "confidence": 0.6, "attestation": "e.java#L1"})
    for r in new["relations"]:
        if r["src"] == "Service:shop" and r["dst"] == "Queue:rabbit:dead":
            r["status"], r["confidence"] = "verified", 0.95
    pages = build_wiki(new, baseline=old)
    compare = pages["compare.md"]
    assert "### Service (1)" in compare and "`extra`" in compare
    assert "KafkaTopic (1)" in compare and "`orders.v2`" in compare
    assert "Service:extra -CONSUMES-> KafkaTopic:orders.v1" in compare
    assert "Service:billing -PRODUCES-> KafkaTopic:orders.v2" in compare
    assert "candidate 0.60 -> verified 0.95" in compare
    assert "+Service:extra" in compare  # orders.v1 consumers changed
    index = pages["index.md"]
    assert "## What changed" in index and "(compare.md)" in index
    assert "compare.md" not in build_wiki(new)


def test_deterministic(tmp_path: Path) -> None:
    manifest, summary = _scanned(tmp_path)
    shuffled = copy.deepcopy(manifest)
    shuffled["relations"].reverse()
    shuffled["entities"].reverse()
    first = build_wiki(manifest, summary=summary, baseline=manifest)
    second = build_wiki(shuffled, summary=summary, baseline=shuffled)
    assert list(first) == list(second)
    assert {k: v.encode() for k, v in first.items()} == {k: v.encode() for k, v in second.items()}


def test_cli_writes_files(tmp_path: Path) -> None:
    manifest, summary = _scanned(tmp_path)
    mpath, spath, opath = tmp_path / "m.json", tmp_path / "s.json", tmp_path / "old.json"
    mpath.write_text(json.dumps(manifest))
    spath.write_text(json.dumps(summary))
    opath.write_text(json.dumps(_hand()))
    out = tmp_path / "wiki"
    runner = CliRunner()
    result = runner.invoke(main, ["ontology", "wiki", str(mpath), "--summary", str(spath),
                                  "--compare", str(opath), "--out", str(out), "--title", "My scan"])
    assert result.exit_code == 0, result.output
    assert (out / "index.md").read_text().startswith("# My scan")
    assert (out / "compare.md").is_file() and (out / "candidates.md").is_file()
    first = {p.name: p.read_bytes() for p in out.rglob("*.md")}
    result = runner.invoke(main, ["ontology", "wiki", str(mpath), "--summary", str(spath),
                                  "--compare", str(opath), "--out", str(out), "--title", "My scan", "--stdout-summary"])
    assert result.exit_code == 0 and "pages to" in result.output and "with problems" in result.output
    assert {p.name: p.read_bytes() for p in out.rglob("*.md")} == first


def test_scan_summary_out(tmp_path: Path) -> None:
    root = _write(tmp_path / "repo", {"svc/build.gradle": "x\n", "svc/Dockerfile": "FROM x\n"})
    out = tmp_path / "scan.json"
    summary = tmp_path / "summary.json"
    result = CliRunner().invoke(main, ["ontology", "scan", str(root), "-o", str(out),
                                       "--summary-out", str(summary)])
    assert result.exit_code == 0, result.output
    assert "kafka" in json.loads(summary.read_text())
    assert "kafka" not in json.loads(out.read_text())
