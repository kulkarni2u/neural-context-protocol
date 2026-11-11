"""Ontology benchmark: baseline NCP vs NCP + ontology on one fixed corpus.

A deterministic, offline workload (design §11 "Performance"): a synthetic
infrastructure estate (services, databases, external APIs, a Kafka topic and
owning teams) where every relation has one evidence chunk in NCP memory,
plus lexically similar distractor chunks (deploy notes, on-call chatter that
name the same services) and unrelated procedure chunks for control tasks.

Two arms run the *same* ``ncp_get_context`` MCP handler with unchanged
arguments, each over its own identically seeded store so that one arm's
reads (retrieval counters) can never shift the other's ranking:

- ``baseline``: no ``[ontology]`` section (the shipped default).
- ``ontology``: ``[ontology] enabled = true`` with the default limits and
  ``retrieval_mode = "auto"``; nothing else changes.

Task families:

- ``dependency``: "what does X depend on" (2 hops). Ground truth is every
  outgoing ``DEPENDS_ON``/``PRODUCES`` relation within two hops of X and its
  evidence chunk.
- ``control``: procedure lookups that name no entity; the ontology trigger
  should skip them, so any cost here is pure regression.

Metrics per arm and family: p50/p95 wall-clock latency of the handler,
prompt tokens of the returned ``context``, *fact* recall (a ground-truth
relation is covered when its evidence chunk is in context or it appears in
the ``[NCP:ONTOLOGY]`` block), *evidence* precision/recall over surfaced
chunk ids (retrieved chunks plus graph ``ev:`` citations), the smallest
prompt that reaches full fact recall on a budget ladder, the unsupported
assertion rate (surfaced relations that are not verified with live
evidence; the corpus plants a candidate relation and a verified relation
whose only evidence was superseded), SQL statements per call (SQLite only,
counted with ``sqlite3.Connection.set_trace_callback``) and the graph cost
the enricher reports (``latency_ms``, ``candidate_count``, ``graph_tokens``).
"""

from __future__ import annotations

import contextlib
import os
import platform
import sqlite3
import statistics
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ncp.config import NCPConfig, load_config
from ncp.mcp.server import make_handlers
from ncp.ontology.facade import OntologyFacade
from ncp.ontology.models import AuthContext, Scope
from ncp.ontology.policy import OntologyConfig
from ncp.tokens import estimate_tokens, token_unit
from ncp.types import SubconsciousChunk

PIPELINE = "bench_ontology"
DEFAULT_BUDGET = 800
LADDER = (200, 300, 400, 600, 800, 1200, 1600, 2400)
_BASE_TIME = 1_760_000_000.0  # fixed chunk timestamps keep ranking reproducible
_HOPS = 2

ENABLED_TOML = "[ontology]\nenabled = true\n"
BASELINE_TOML = "# baseline: no [ontology] section (the shipped default)\n"

# -- the synthetic estate --------------------------------------------------------

SERVICES = (
    "edge-gateway", "checkout", "cart", "payments", "fraud-check", "ledger-writer",
    "inventory", "pricing", "catalog", "search", "shipping", "notifications",
)
DATABASES = ("orders-db", "ledger-db", "inventory-db", "catalog-db")
APIS = ("card-network-api", "carrier-api")
TOPICS = ("order-events",)
TEAMS = {
    "team-commerce": ("edge-gateway", "checkout", "cart", "pricing", "catalog", "search"),
    "team-payments": ("payments", "fraud-check", "ledger-writer"),
    "team-logistics": ("inventory", "shipping", "notifications"),
}
DEPENDS_ON = (
    ("edge-gateway", "checkout"), ("edge-gateway", "search"),
    ("checkout", "cart"), ("checkout", "payments"), ("checkout", "pricing"),
    ("checkout", "orders-db"),
    ("cart", "pricing"), ("cart", "inventory"),
    ("payments", "fraud-check"), ("payments", "ledger-writer"), ("payments", "card-network-api"),
    ("fraud-check", "ledger-db"), ("ledger-writer", "ledger-db"),
    ("inventory", "inventory-db"), ("pricing", "catalog"), ("catalog", "catalog-db"),
    ("search", "catalog"), ("shipping", "carrier-api"), ("shipping", "inventory"),
)
PRODUCES = (("checkout", "order-events"),)
CONSUMES = (("shipping", "order-events"), ("notifications", "order-events"))
# Planted hazards: neither may ever surface as a verified fact.
CANDIDATE_RELATION = ("search", "payments")       # asserted as candidate only
STALE_RELATION = ("notifications", "payments")    # verified, evidence superseded

DEPENDENCY_SEEDS = (
    "checkout", "payments", "cart", "shipping", "edge-gateway", "pricing", "fraud-check",
    "inventory",
)

_DEPENDS_TEMPLATES = (
    "{src} calls {dst} synchronously on the request path; its deploy manifest sets {env}_URL.",
    "Runbook note: {src} cannot serve traffic when {dst} is down, it depends on {dst}.",
    "Architecture review: {src} uses {dst} for every request (client library pinned in {src}).",
)
_DISTRACTOR_TEMPLATES = (
    "{svc} deploy rolled out build 1.{i}.0 to prod; canary metrics were green.",
    "{svc} on-call handoff: dashboards for {svc} moved to the new folder, no open pages.",
    "{svc} flaky integration test quarantined after three retries in CI this week.",
)
CONTROL_TOPICS = {
    "rotate_tls_certificates": (
        "Procedure: rotate TLS certificates with the cert-manager renew job, then reload ingress.",
        "TLS certificates expire after 90 days; rotation must finish 14 days before expiry.",
    ),
    "log_retention_policy": (
        "Log retention policy: application logs are kept 30 days, audit logs 400 days.",
        "Retention is enforced by the nightly lifecycle rule on the logs bucket.",
    ),
    "oncall_escalation_rules": (
        "On-call escalation: page the secondary after 15 minutes without acknowledgement.",
        "Escalation to the incident commander happens for any customer-visible outage.",
    ),
    "code_review_policy": (
        "Code review policy: two approvals are required for changes to shared libraries.",
        "Reviewers must check migrations for backwards-compatible rollouts before approving.",
    ),
    "feature_flag_cleanup": (
        "Feature flag cleanup: flags older than 60 days are listed in the weekly hygiene report.",
        "Remove a stale flag by deleting its config entry and the guarded dead code together.",
    ),
    "postmortem_template": (
        "Postmortem template sections: summary, impact, timeline, root cause, action items.",
        "Postmortems are due five business days after the incident is resolved.",
    ),
}
_FILLER = (
    "Quarterly planning notes: the roadmap review moved to Thursday.",
    "Office wifi maintenance is scheduled for Saturday morning.",
    "Reminder: update your laptop OS before the end of the month.",
    "The design guild meets every other Wednesday in the large room.",
)


@dataclass
class Fact:
    relation_type: str
    src: str
    dst: str
    chunk_id: str
    content: str = ""
    relation_id: str = ""
    supported: bool = True  # verified with live evidence


@dataclass
class Corpus:
    """Ground truth for one seeded store."""

    facts: list[Fact] = field(default_factory=list)
    control_chunks: dict[str, tuple[str, ...]] = field(default_factory=dict)
    chunk_count: int = 0

    def by_relation_id(self) -> dict[str, Fact]:
        return {fact.relation_id: fact for fact in self.facts}

    def dependency_truth(self, seed: str) -> list[Fact]:
        """Outgoing DEPENDS_ON/PRODUCES facts within ``_HOPS`` hops of ``seed``."""
        outgoing = [f for f in self.facts if f.supported and f.relation_type in
                    ("DEPENDS_ON", "PRODUCES")]
        frontier, seen, found = {seed}, {seed}, []
        for _ in range(_HOPS):
            step = [f for f in outgoing if f.src in frontier and f not in found]
            found.extend(step)
            frontier = {f.dst for f in step} - seen
            seen |= frontier
        return found


def _entity_type(key: str) -> str:
    if key in DATABASES:
        return "Database"
    if key in APIS:
        return "API"
    if key in TOPICS:
        return "KafkaTopic"
    if key in TEAMS:
        return "Team"
    return "Service"


def _attributes(key: str) -> dict[str, object]:
    kind = _entity_type(key)
    if kind == "Service":
        return {"name": key, "environment": "prod"}
    if kind == "KafkaTopic":
        return {"cluster": "main", "name": key}
    return {"name": key}


def seed_corpus(store: Any, facade: OntologyFacade) -> Corpus:
    """Write the memory chunks and the verified graph into ``store``."""
    corpus = Corpus()
    clock = iter(range(10_000))

    def chunk(chunk_id: str, content: str) -> str:
        written = SubconsciousChunk(
            chunk_id=chunk_id, layer="semantic", content=content, src="tool_result",
            pipeline_id=PIPELINE, written_by="bench", created_at=_BASE_TIME + next(clock),
        )
        if not store.write(written, allow_duplicate=True):
            raise RuntimeError(f"benchmark chunk {chunk_id} was not written")
        corpus.chunk_count += 1
        return chunk_id

    auth = AuthContext(
        principal_id="bench", scope=Scope(tenant_id="default", namespace=PIPELINE),
        capabilities=frozenset({"read", "write", "verify"}),
    )
    observed = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    keys = [*SERVICES, *DATABASES, *APIS, *TOPICS, *TEAMS]
    entity_ids: dict[str, str] = {}
    for key in keys:
        result = facade.assert_entity(auth, {
            "entity_type": _entity_type(key), "canonical_key": key, "display_name": key,
            "attributes": _attributes(key),
        })
        entity_ids[key] = str(result["entity"]["entity_id"])  # type: ignore[index]

    def relate(fact: Fact, status: str = "verified") -> None:
        result = facade.assert_relation(auth, {
            "src_entity_id": entity_ids[fact.src],
            "dst_entity_id": entity_ids[fact.dst],
            "relation_type": fact.relation_type, "status": status, "confidence": 0.9,
            "observed_at": observed,
            "evidence": [{"evidence_type": "chunk", "evidence_id": fact.chunk_id,
                          "source_revision": "1", "observed_at": observed}],
        }, idempotency_key=f"{fact.src}-{fact.relation_type}-{fact.dst}")
        fact.relation_id = str(result["relation"]["relation_id"])  # type: ignore[index]
        corpus.facts.append(fact)

    for index, (src, dst) in enumerate(DEPENDS_ON):
        text = _DEPENDS_TEMPLATES[index % len(_DEPENDS_TEMPLATES)].format(
            src=src, dst=dst, env=dst.upper().replace("-", "_"))
        relate(Fact("DEPENDS_ON", src, dst, chunk(f"ev-dep-{index:02d}", text), text))
    for index, (src, dst) in enumerate(PRODUCES):
        text = f"{src} publishes every placed order to the {dst} Kafka topic."
        relate(Fact("PRODUCES", src, dst, chunk(f"ev-prod-{index:02d}", text), text))
    for index, (src, dst) in enumerate(CONSUMES):
        text = f"{src} runs a consumer group on the {dst} Kafka topic."
        relate(Fact("CONSUMES", src, dst, chunk(f"ev-cons-{index:02d}", text), text))
    for team, services in TEAMS.items():
        for index, service in enumerate(services):
            text = f"{team} owns {service}; escalations go to the {team} rotation."
            relate(Fact("OWNS", team, service, chunk(f"ev-own-{team}-{index:02d}", text), text))

    src, dst = CANDIDATE_RELATION
    text = f"Unconfirmed: someone thought {src} might call {dst} for gift cards."
    relate(Fact("DEPENDS_ON", src, dst, chunk("ev-candidate-00", text), supported=False),
           status="candidate")
    src, dst = STALE_RELATION
    stale = Fact("DEPENDS_ON", src, dst,
                 chunk("ev-stale-00", f"{src} calls {dst} to look up refund state."),
                 supported=False)
    relate(stale)
    replacement = chunk("ev-stale-01", f"{src} no longer calls {dst}; refunds come via events.")
    store.supersede(stale.chunk_id, replacement)

    for service_index, service in enumerate(SERVICES):
        for index, template in enumerate(_DISTRACTOR_TEMPLATES):
            chunk(f"noise-{service}-{index}", template.format(svc=service, i=service_index))
    for topic, texts in CONTROL_TOPICS.items():
        corpus.control_chunks[topic] = tuple(
            chunk(f"ctl-{topic}-{index}", text) for index, text in enumerate(texts)
        )
    for index, text in enumerate(_FILLER * 3):
        chunk(f"filler-{index:02d}", f"{text} (note {index})")
    return corpus


def _memory_line(fact: Fact) -> str:
    """The fact's evidence chunk as it would appear in ``[NCP:SUBCONSCIOUS]``."""
    return (f"chunk:{fact.chunk_id} layer:semantic score:0.90 src:tool_result trust:0.7\n"
            f"  {fact.content}\n")


# -- tasks -----------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    family: str  # "dependency" | "control"
    name: str
    args: dict[str, object]
    truth_facts: tuple[str, ...]      # relation ids (dependency tasks)
    truth_chunks: tuple[str, ...]     # evidence chunk ids
    oracle_tokens: int | None = None  # tokens to show exactly the truth evidence as memory


def build_tasks(corpus: Corpus, *, seeds: tuple[str, ...] = DEPENDENCY_SEEDS,
                controls: tuple[str, ...] | None = None) -> list[Task]:
    tasks: list[Task] = []
    for seed in seeds:
        slug = seed.replace("-", "_")
        truth = corpus.dependency_truth(seed)
        tasks.append(Task(
            family="dependency", name=f"deps:{seed}",
            args={"task": f"map_{slug}_dependencies", "slot": slug,
                  "intent": "list_upstream_dependencies_two_hops"},
            truth_facts=tuple(f.relation_id for f in truth),
            truth_chunks=tuple(f.chunk_id for f in truth),
            oracle_tokens=sum(estimate_tokens(_memory_line(f)) for f in truth),
        ))
    for topic in controls if controls is not None else tuple(corpus.control_chunks):
        tasks.append(Task(
            family="control", name=f"ctl:{topic}",
            args={"task": topic, "slot": "procedure", "intent": "follow_documented_procedure"},
            truth_facts=(), truth_chunks=corpus.control_chunks[topic],
        ))
    return tasks


# -- measurement -----------------------------------------------------------------


class _StatementCounter:
    """Counts SQL statements on every sqlite3 connection opened while active."""

    def __init__(self) -> None:
        self.count = 0

    @contextlib.contextmanager
    def active(self) -> Iterator[None]:
        original = sqlite3.connect

        def traced(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connection = original(*args, **kwargs)
            connection.set_trace_callback(self._tick)
            return connection

        sqlite3.connect = traced  # type: ignore[assignment]
        try:
            yield
        finally:
            sqlite3.connect = original  # type: ignore[assignment]

    def _tick(self, _statement: str) -> None:
        self.count += 1


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return ordered[int(rank)]


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _surfaced(response: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(chunk ids in context or cited by the graph block, relation ids in the block)."""
    chunks = {str(item) for item in response.get("retrieved_chunk_ids") or ()}
    relations: set[str] = set()
    meta = response.get("ontology") or {}
    for path in meta.get("paths") or ():
        chunks.update(str(item) for item in path.get("evidence_ids") or ())
        relations.update(str(item) for item in path.get("relation_ids") or ())
    return chunks, relations


def _fact_recall(task: Task, corpus_facts: dict[str, Fact], chunks: set[str],
                 relations: set[str]) -> float | None:
    if not task.truth_facts:
        return None
    covered = sum(1 for rid in task.truth_facts
                  if rid in relations or corpus_facts[rid].chunk_id in chunks)
    return covered / len(task.truth_facts)


Handler = Callable[[dict[str, object]], Any]


def _call(handler: Handler, task: Task, budget: int, agent: str) -> dict[str, Any]:
    payload = {"agent_id": agent, "role": "bench", "pipeline_id": PIPELINE,
               "max_tokens": budget, **task.args}
    response = handler(payload)
    if not isinstance(response, dict) or "context" not in response:
        raise RuntimeError(f"unexpected ncp_get_context response for {task.name}")
    return response


def _run_arm(
    handler: Handler, tasks: list[Task], corpus: Corpus, *, budget: int, repeats: int,
    ladder: tuple[int, ...], sqlite_backend: bool,
) -> dict[str, Any]:
    facts = corpus.by_relation_id()
    unsupported_ids = {f.relation_id for f in corpus.facts if not f.supported}
    counter = _StatementCounter()
    per_task: list[dict[str, Any]] = []
    surfaced_relations = 0
    unsupported_surfaced = 0

    # Pass 1 (first, on a fresh store): quality, tokens, graph cost, SQL count.
    for index, task in enumerate(tasks):
        before = counter.count
        if sqlite_backend:
            with counter.active():
                response = _call(handler, task, budget, f"bench-q{index}")
        else:
            response = _call(handler, task, budget, f"bench-q{index}")
        chunks, relations = _surfaced(response)
        truth = set(task.truth_chunks)
        hit = len(chunks & truth)
        meta = response.get("ontology") or {}
        surfaced_relations += len(relations)
        unsupported_surfaced += len(relations & unsupported_ids)
        # A relation in the block must be one the corpus verified with live evidence.
        unsupported_surfaced += len([rid for rid in relations if rid not in facts])
        per_task.append({
            "task": task.name, "family": task.family,
            "prompt_tokens": estimate_tokens(str(response["context"])),
            "fact_recall": _fact_recall(task, facts, chunks, relations),
            "evidence_recall": hit / len(truth) if truth else None,
            "evidence_precision": hit / len(chunks) if chunks else 0.0,
            "surfaced_chunks": len(chunks),
            "graph_relations": len(relations),
            "sql_statements": counter.count - before if sqlite_backend else None,
            "ontology_triggered": bool(meta.get("ontology_triggered")),
            "reason_code": meta.get("reason_code"),
            "fallback_reason": meta.get("fallback_reason"),
            "graph_latency_ms": meta.get("latency_ms"),
            "graph_tokens": meta.get("graph_tokens"),
            "graph_candidates": meta.get("candidate_count"),
            "oracle_evidence_tokens": task.oracle_tokens,
        })

    # Pass 2: smallest prompt reaching full fact recall on the budget ladder.
    for index, task in enumerate(tasks):
        row = per_task[index]
        row["tokens_to_full_recall"] = None
        row["budget_to_full_recall"] = None
        if task.family != "dependency":
            continue
        for rung in ladder:
            response = _call(handler, task, rung, f"bench-l{index}-{rung}")
            chunks, relations = _surfaced(response)
            if _fact_recall(task, facts, chunks, relations) == 1.0:
                row["tokens_to_full_recall"] = estimate_tokens(str(response["context"]))
                row["budget_to_full_recall"] = rung
                break

    # Pass 3: latency (untraced). One warm-up call per task, then repeats.
    latencies: dict[str, list[float]] = {"dependency": [], "control": []}
    for index, task in enumerate(tasks):
        _call(handler, task, budget, f"bench-w{index}")
    for round_index in range(repeats):
        for index, task in enumerate(tasks):
            started = time.perf_counter()
            _call(handler, task, budget, f"bench-t{index}-{round_index}")
            latencies[task.family].append((time.perf_counter() - started) * 1000)

    families: dict[str, Any] = {}
    for family in ("dependency", "control"):
        rows = [row for row in per_task if row["family"] == family]
        if not rows:
            continue

        def mean(key: str, rows: list[dict[str, Any]] = rows) -> float | None:
            values = [row[key] for row in rows if row[key] is not None]
            return _round(statistics.fmean(values)) if values else None

        reach = [row["tokens_to_full_recall"] for row in rows
                 if row["tokens_to_full_recall"] is not None]
        families[family] = {
            "tasks": len(rows),
            "latency_ms_p50": _round(_percentile(latencies[family], 50)),
            "latency_ms_p95": _round(_percentile(latencies[family], 95)),
            "latency_samples": len(latencies[family]),
            "prompt_tokens_mean": mean("prompt_tokens"),
            "fact_recall_mean": mean("fact_recall"),
            "evidence_recall_mean": mean("evidence_recall"),
            "evidence_precision_mean": mean("evidence_precision"),
            "sql_statements_mean": mean("sql_statements"),
            "triggered": sum(1 for row in rows if row["ontology_triggered"]),
            "fallbacks": sum(1 for row in rows if row["fallback_reason"]),
            "graph_latency_ms_mean": mean("graph_latency_ms"),
            "graph_tokens_mean": mean("graph_tokens"),
            "oracle_evidence_tokens_mean": mean("oracle_evidence_tokens"),
            "full_recall_reached": len(reach),
            "tokens_to_full_recall_mean": _round(statistics.fmean(reach)) if reach else None,
        }
    return {
        "families": families,
        "surfaced_relations": surfaced_relations,
        "unsupported_surfaced": unsupported_surfaced,
        "unsupported_assertion_rate": (
            _round(unsupported_surfaced / surfaced_relations, 4) if surfaced_relations else 0.0
        ),
        "tasks": per_task,
    }


# -- backends --------------------------------------------------------------------


@dataclass
class _Arm:
    name: str
    handler: Handler
    corpus: Corpus
    close: Callable[[], None]


def _project(root: Path, toml: str) -> NCPConfig:
    (root / ".ncp").mkdir(parents=True, exist_ok=True)
    path = root / ".ncp" / "config.toml"
    path.write_text(toml, encoding="utf-8")
    return load_config(path, cwd=root, env={})


def _seeded(store: Any) -> Corpus:
    seeding = OntologyFacade.from_config(OntologyConfig(enabled=True), store)
    if not seeding.enabled:
        raise RuntimeError(f"ontology is not available for {type(store).__name__}")
    return seed_corpus(store, seeding)


def _sqlite_arm(root: Path, name: str, toml: str) -> _Arm:
    from ncp.stores.sqlite import SQLiteStore

    config = _project(root / name, toml)
    store = SQLiteStore(root / name / ".ncp" / "store.db")
    corpus = _seeded(store)
    handler = make_handlers(store, config=config)["ncp_get_context"]
    return _Arm(name, handler, corpus, lambda: None)


def _postgres_arm(root: Path, name: str, toml: str, dsn: str, redis_url: str) -> _Arm:
    import psycopg

    from ncp.stores.pgvector import PgvectorStore

    config = _project(root / name, toml)
    schema = f"ncp_bench_ont_{uuid4().hex[:10]}"
    # Context assembly peeks whispers, which pgvector keeps in Redis; a
    # per-run stream keeps runs from seeing each other's whispers.
    store = PgvectorStore(dsn, schema=schema, table_prefix="bench_", redis_url=redis_url,
                          redis_stream=f"ncp:bench:{schema}:whispers")

    def close() -> None:
        store.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")

    try:
        corpus = _seeded(store)
    except Exception:
        close()
        raise
    handler = make_handlers(store, config=config)["ncp_get_context"]
    return _Arm(name, handler, corpus, close)


def _compare(baseline: dict[str, Any], ontology: dict[str, Any]) -> dict[str, Any]:
    def family(arm: dict[str, Any], name: str) -> dict[str, Any]:
        return arm["families"].get(name, {})

    def delta(name: str, key: str) -> float | None:
        base, ont = family(baseline, name).get(key), family(ontology, name).get(key)
        return None if base is None or ont is None else _round(ont - base)

    def reduction(name: str, key: str) -> float | None:
        base, ont = family(baseline, name).get(key), family(ontology, name).get(key)
        return None if not base or ont is None else _round(1 - ont / base, 4)

    # Paired: tasks where both arms reach full recall on the ladder.
    pairs = [
        (b["tokens_to_full_recall"], o["tokens_to_full_recall"])
        for b, o in zip(baseline["tasks"], ontology["tasks"])
        if b["family"] == "dependency" and b["tokens_to_full_recall"] is not None
        and o["tokens_to_full_recall"] is not None
    ]
    paired = (_round(1 - sum(o for _, o in pairs) / sum(b for b, _ in pairs), 4)
              if pairs else None)
    # Graph block vs the evidence text a perfect memory retriever would show.
    graph_vs_oracle = [
        (o["graph_tokens"], o["oracle_evidence_tokens"]) for o in ontology["tasks"]
        if o["family"] == "dependency" and o["graph_tokens"] and o["oracle_evidence_tokens"]
    ]
    graph_oracle_reduction = (
        _round(1 - sum(g for g, _ in graph_vs_oracle) / sum(t for _, t in graph_vs_oracle), 4)
        if graph_vs_oracle else None
    )
    p95_overhead = delta("dependency", "latency_ms_p95")
    control_p95 = delta("control", "latency_ms_p95")
    return {
        "dependency_p95_overhead_ms": p95_overhead,
        "dependency_p50_overhead_ms": delta("dependency", "latency_ms_p50"),
        "control_p95_overhead_ms": control_p95,
        "dependency_fixed_budget_token_reduction": reduction("dependency", "prompt_tokens_mean"),
        "dependency_tokens_to_full_recall_reduction_paired": paired,
        "dependency_tokens_to_full_recall_pairs": len(pairs),
        "dependency_full_recall_reached": {
            "baseline": family(baseline, "dependency").get("full_recall_reached"),
            "ontology": family(ontology, "dependency").get("full_recall_reached"),
        },
        "dependency_graph_vs_oracle_evidence_token_reduction": graph_oracle_reduction,
        "dependency_fact_recall_delta": delta("dependency", "fact_recall_mean"),
        "dependency_evidence_precision_delta": delta("dependency", "evidence_precision_mean"),
        "control_prompt_token_delta": delta("control", "prompt_tokens_mean"),
        "control_evidence_recall_delta": delta("control", "evidence_recall_mean"),
        "unsupported_assertion_rate": ontology["unsupported_assertion_rate"],
        "hypotheses": {
            "p95_overhead_under_50ms": None if p95_overhead is None else p95_overhead < 50,
            "token_reduction_15_to_25pct_dependency": (
                None if paired is None else paired >= 0.15
            ),
            "no_control_regression": (
                delta("control", "evidence_recall_mean") in (0, 0.0, None)
                and (delta("control", "prompt_tokens_mean") or 0) <= 0
            ),
            "zero_unsupported_assertions": ontology["unsupported_surfaced"] == 0,
        },
    }


def run_ontology_benchmark(
    *,
    workdir: str | Path,
    backends: tuple[str, ...] = ("sqlite",),
    budget: int = DEFAULT_BUDGET,
    repeats: int = 15,
    ladder: tuple[int, ...] = LADDER,
    seeds: tuple[str, ...] = DEPENDENCY_SEEDS,
    controls: tuple[str, ...] | None = None,
    dsn: str | None = None,
    redis_url: str | None = None,
    ontology_toml: str = ENABLED_TOML,
) -> dict[str, Any]:
    """Run both arms on each backend; return a JSON-serializable artifact."""
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    for backend in backends:
        arms: dict[str, Any] = {}
        for name, toml in (("baseline", BASELINE_TOML), ("ontology", ontology_toml)):
            if backend == "sqlite":
                arm = _sqlite_arm(root / backend, name, toml)
            elif backend == "postgres":
                if not dsn:
                    raise ValueError("the postgres backend needs a DSN")
                arm = _postgres_arm(root / backend, name, toml, dsn,
                                    redis_url or "redis://127.0.0.1:6379/0")
            else:
                raise ValueError(f"unknown backend {backend!r}")
            try:
                tasks = build_tasks(arm.corpus, seeds=seeds, controls=controls)
                arms[name] = _run_arm(
                    arm.handler, tasks, arm.corpus, budget=budget, repeats=repeats,
                    ladder=ladder, sqlite_backend=backend == "sqlite",
                )
                arms[name]["corpus"] = {
                    "chunks": arm.corpus.chunk_count,
                    "relations": len(arm.corpus.facts),
                    "entities": len(SERVICES) + len(DATABASES) + len(APIS) + len(TOPICS)
                    + len(TEAMS),
                }
            finally:
                arm.close()
        results[backend] = {"arms": arms, "comparison": _compare(arms["baseline"],
                                                                arms["ontology"])}
    return {
        "benchmark": "ontology",
        "token_unit": token_unit(),
        "config": {
            "budget_tokens": budget, "repeats": repeats, "ladder": list(ladder),
            "dependency_seeds": list(seeds), "hops_in_ground_truth": _HOPS,
            "ontology_toml": ontology_toml.strip(),
        },
        "environment": {
            "python": sys.version.split()[0], "platform": platform.platform(),
            "cpu_count": os.cpu_count(), "sqlite": sqlite3.sqlite_version,
        },
        "backends": results,
    }
