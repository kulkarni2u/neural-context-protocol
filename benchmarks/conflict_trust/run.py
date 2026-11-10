#!/usr/bin/env python3
"""Conflicting and low-trust memory benchmark.

The other benchmarks plant one true fact and ask whether it survives into a
bounded context. A lexical retriever solves that as well as NCP does. This one
plants a true fact *and* contradicting claims that match the query equally
well, so the lexical signal cannot tell them apart; what separates them is
provenance: source trust, a ``supersedes`` correction, recorded outcomes,
dissent, or repetition by one author.

Every conflicting claim in a task is the same sentence with a different value,
so BM25 scores them identically. Each arm then gets the same chunk budget
``k``:

- ``ncp``: the assembler's bounded context.
- ``plain_rag``: BM25 over every written text, packed into ``k`` chunks'
  worth of tokens. Its documents are presented in a seeded random order, so a
  BM25 tie is broken at random rather than for or against the true fact.
- ``sliding_window``: the last ``k`` texts written.
- ``raw_replay``: every text written, in order (unbounded).

Scenario families:

- ``no_conflict``: control. Only the true fact; every bounded retriever should
  find it.
- ``low_trust_distractor``: the true fact is ``tool_result``; contradicting
  ``agent_inferred`` guesses come from other agents.
- ``superseded``: an old value is corrected by a chunk that ``supersedes`` it.
- ``outcome_feedback``: two equal-trust claims; outcomes are recorded for and
  against them and ``calibrate`` runs.
- ``dissent``: two equal-trust claims; reviewers dissent against the wrong one
  and ``calibrate`` runs.
- ``repetition_flood``: one low-trust author repeats the wrong claim.
- ``misplaced_trust``: the *wrong* claim carries the higher trust and nothing
  corrects it. NCP is expected to fail here: trust weighting helps only when
  trust tracks truth.

Metrics, per arm and scenario, averaged over tasks:

- ``answer_present``: the true fact is in the context.
- ``conflict_present``: a contradicting claim is in the context.
- ``clean``: the true fact is present and no contradicting claim is.
- ``top1_correct``: the first claim in the context is the true fact. This
  models a reader that acts on the highest-ranked claim.

Deterministic, no LLM, no network. It measures what reaches the context, not
whether a model then answers correctly.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import random
import tempfile

from ncp.api import agent
from ncp.assembler import Assembler
from ncp.bench.baselines import PlainRagBaseline
from ncp.benchmarks import estimate_tokens, token_unit
from ncp.stores.sqlite import SQLiteStore
from ncp.types import BudgetContext, OutcomeRecord, SubconsciousChunk

ARMS = ("ncp", "plain_rag", "sliding_window", "raw_replay")
METRICS = ("answer_present", "conflict_present", "clean", "top1_correct")
SCENARIOS = (
    "no_conflict",
    "low_trust_distractor",
    "superseded",
    "outcome_feedback",
    "dissent",
    "repetition_flood",
    "misplaced_trust",
)
# Scenarios where the store holds a signal that says which claim is right.
RESOLVABLE = (
    "low_trust_distractor",
    "superseded",
    "outcome_feedback",
    "dissent",
    "repetition_flood",
)


@dataclass(frozen=True, slots=True)
class Fact:
    """One question with a true value and contradicting values.

    ``statement`` holds a ``{value}`` placeholder; every claim about the fact
    is that sentence with a different value, so claims differ by one token.
    """

    key: str
    question: str
    statement: str
    true_value: str
    wrong_values: tuple[str, ...]


FACTS: tuple[Fact, ...] = (
    Fact("retry_limit", "what is the retry limit for the payment gateway client",
         "The retry limit for the payment gateway client is {value} attempts.", "3", ("7", "9", "12", "15")),
    Fact("deploy_window", "which weekday is the deploy window for the billing service",
         "The deploy window for the billing service is on {value} morning.", "Tuesday", ("Friday", "Sunday", "Monday", "Thursday")),
    Fact("cache_ttl", "what is the cache ttl for session tokens in seconds",
         "The cache ttl for session tokens is {value} seconds.", "900", ("60", "3600", "30", "86400")),
    Fact("upload_cap", "what is the maximum upload size for attachments in megabytes",
         "The maximum upload size for attachments is {value} megabytes.", "25", ("500", "100", "5", "250")),
    Fact("export_owner", "which team owns the invoice export job",
         "The invoice export job is owned by the {value} team.", "finance", ("growth", "search", "identity", "mobile")),
    Fact("db_region", "which region hosts the primary orders database",
         "The primary orders database is hosted in the {value} region.", "frankfurt", ("oregon", "sydney", "mumbai", "ohio")),
    Fact("queue_name", "which queue receives refund events",
         "Refund events are published to the {value} queue.", "ledger", ("audit", "mailer", "search", "archive")),
    Fact("rate_limit", "what is the rate limit for the public search endpoint per minute",
         "The rate limit for the public search endpoint is {value} requests per minute.", "120", ("600", "30", "1000", "45")),
    Fact("log_retention", "how many days are access logs retained",
         "Access logs are retained for {value} days.", "30", ("365", "7", "90", "14")),
    Fact("feature_flag", "which flag gates the new checkout flow",
         "The new checkout flow is gated by the {value} flag.", "aurora", ("borealis", "cobalt", "delta", "ember")),
    Fact("oncall_rota", "which rota is paged for webhook delivery failures",
         "Webhook delivery failures page the {value} rota.", "integrations", ("platform", "payments", "data", "support")),
    Fact("tls_version", "what is the minimum tls version accepted by the edge proxy",
         "The edge proxy accepts tls version {value} as its minimum.", "1.3", ("1.0", "1.1", "1.2", "1.25")),
)

_FILLER_TOPICS = (
    "the lint configuration was tidied and import ordering now follows the house style",
    "a flaky snapshot comparison in the dashboard suite was stabilised with a fixed clock",
    "the changelog entry for the navigation refactor was reworded for clarity",
    "an unused helper in the string formatting module was removed after review",
    "the staging banner colour was adjusted to meet the contrast guideline",
    "a typo in the onboarding tooltip copy was corrected",
    "the icon sprite sheet was regenerated after two glyphs were renamed",
    "pagination controls on the admin table gained keyboard focus styles",
)


@dataclass(slots=True)
class _Planted:
    """What a task wrote: texts in write order, and which are true or wrong."""

    transcript: list[str] = field(default_factory=list)
    true_text: str = ""
    wrong_texts: set[str] = field(default_factory=set)
    true_ids: set[str] = field(default_factory=set)
    wrong_ids: set[str] = field(default_factory=set)
    suppressed_writes: list[str] = field(default_factory=list)


def _chunk(
    chunk_id: str,
    content: str,
    *,
    src: str,
    trust: float,
    written_by: str,
    pipeline_id: str,
    supersedes: str | None = None,
) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=chunk_id,
        layer="semantic",
        content=content,
        src=src,
        base_trust=trust,
        written_by=written_by,
        pipeline_id=pipeline_id,
        supersedes=supersedes,
    )


def _plant(store: SQLiteStore, scenario: str, fact: Fact, *, pipeline_id: str, rng: random.Random,
           distractors: int, fillers: int) -> _Planted:
    planted = _Planted()
    true_text = fact.statement.format(value=fact.true_value)
    wrong = [fact.statement.format(value=value) for value in fact.wrong_values]
    planted.true_text = true_text

    def write(chunk_id: str, text: str, *, is_true: bool | None, **kwargs: object) -> None:
        written = store.write(_chunk(chunk_id, text, pipeline_id=pipeline_id, **kwargs))  # type: ignore[arg-type]
        # The baselines read the raw transcript, so they see every write, even
        # one the store suppressed as a near-duplicate.
        planted.transcript.append(text)
        if not written:
            planted.suppressed_writes.append(chunk_id)
        if is_true is True:
            planted.true_ids.add(chunk_id)
        elif is_true is False:
            planted.wrong_ids.add(chunk_id)
            planted.wrong_texts.add(text)

    if scenario == "no_conflict":
        write("fact_true", true_text, is_true=True, src="tool_result", trust=0.8, written_by="verifier")

    elif scenario == "low_trust_distractor":
        # Seeded order: the true fact is not always first or last.
        claims: list[tuple[str, str, bool]] = [("fact_true", true_text, True)]
        claims += [(f"fact_wrong_{i}", wrong[i % len(wrong)], False) for i in range(distractors)]
        rng.shuffle(claims)
        for chunk_id, text, is_true in claims:
            if is_true:
                write(chunk_id, text, is_true=True, src="tool_result", trust=0.8, written_by="verifier")
            else:
                write(chunk_id, text, is_true=False, src="agent_inferred", trust=0.3, written_by=f"guesser_{chunk_id[-1]}")

    elif scenario == "superseded":
        # The old value was correct when written; the correction replaces it.
        write("fact_old", wrong[0], is_true=False, src="tool_result", trust=0.8, written_by="verifier")
        write("fact_true", true_text, is_true=True, src="tool_result", trust=0.8, written_by="verifier", supersedes="fact_old")

    elif scenario in ("outcome_feedback", "dissent", "misplaced_trust"):
        pair: list[tuple[str, str, bool]] = [("fact_true", true_text, True), ("fact_wrong_0", wrong[0], False)]
        rng.shuffle(pair)
        for chunk_id, text, is_true in pair:
            if scenario == "misplaced_trust":
                src, trust = ("agent_inferred", 0.6) if is_true else ("user_verified", 0.95)
            else:
                src, trust = "synthesis", 0.7
            write(chunk_id, text, is_true=is_true, src=src, trust=trust, written_by="agent_a" if is_true else "agent_b")
        if scenario == "outcome_feedback":
            for index in range(2):
                store.record_outcome(OutcomeRecord(outcome_id=f"out_ok_{index}", chunk_ids=["fact_true"], success=True))
                store.record_outcome(OutcomeRecord(outcome_id=f"out_bad_{index}", chunk_ids=["fact_wrong_0"], success=False))
            store.calibrate(pipeline_id=pipeline_id, feedback_mode=True)
        elif scenario == "dissent":
            for index in range(3):
                store.record_dissent("fact_wrong_0", identity_id=f"reviewer_{index}")
            store.calibrate(pipeline_id=pipeline_id, feedback_mode=True)

    elif scenario == "repetition_flood":
        claims = [("fact_true", true_text, True)]
        claims += [(f"fact_wrong_{i}", wrong[0], False) for i in range(distractors)]
        rng.shuffle(claims)
        for chunk_id, text, is_true in claims:
            if is_true:
                write(chunk_id, text, is_true=True, src="tool_result", trust=0.8, written_by="verifier")
            else:
                write(chunk_id, text, is_true=False, src="agent_inferred", trust=0.5, written_by="repeater")
    else:
        raise ValueError(f"unknown scenario: {scenario}")

    for index in range(fillers):
        topic = _FILLER_TOPICS[index % len(_FILLER_TOPICS)]
        write(f"filler_{index}", f"Note {index + 1}: {topic}.", is_true=None, src="synthesis", trust=0.55, written_by="worker")
    return planted


def _score(items: list[str], *, true_items: set[str], wrong_items: set[str]) -> dict[str, bool]:
    """Score an ordered context. ``items`` are chunk ids (NCP) or texts (baselines)."""
    answer = any(item in true_items for item in items)
    conflict = any(item in wrong_items for item in items)
    claims = [item for item in items if item in true_items or item in wrong_items]
    return {
        "answer_present": answer,
        "conflict_present": conflict,
        "clean": answer and not conflict,
        "top1_correct": bool(claims) and claims[0] in true_items,
    }


def _run_task(scenario: str, fact: Fact, *, store_path: Path, k: int, seed: int,
              distractors: int, fillers: int) -> dict[str, object]:
    pipeline_id = f"bench_conflict_{scenario}_{fact.key}"
    rng = random.Random(f"{seed}:{scenario}:{fact.key}")
    store = SQLiteStore(store_path)
    planted = _plant(store, scenario, fact, pipeline_id=pipeline_id, rng=rng, distractors=distractors, fillers=fillers)

    conscious = agent(
        id="reader",
        role="review",
        owns=["answers"],
        must_not=["shipping"],
        task=f"answer_{fact.key}",
        slot="facts",
        intent="answer_question",
        pipeline_id=pipeline_id,
        recent=[],
        steps_completed=1,
        steps_total=2,
    )
    assembly = Assembler(store=store).assemble(
        conscious=conscious,
        budget=BudgetContext(ctx_used=0.3, steps_completed=1, steps_total=2, elapsed_seconds=5.0, pressure="medium"),
        query_text=fact.question,
        k=k,
    )
    ncp_ids = assembly.stored_evidence_ids()

    transcript = planted.transcript
    # Shuffle what BM25 sees so identical scores tie-break at random.
    rag_docs = list(dict.fromkeys(transcript))
    rng.shuffle(rag_docs)
    rag_budget = k * max(estimate_tokens(text) for text in transcript)
    rag_items = PlainRagBaseline(budget_tokens=rag_budget).select(transcript=rag_docs, query=fact.question)[:k]

    true_texts = {planted.true_text}
    results = {
        "ncp": _score(ncp_ids, true_items=planted.true_ids, wrong_items=planted.wrong_ids),
        "plain_rag": _score(rag_items, true_items=true_texts, wrong_items=planted.wrong_texts),
        "sliding_window": _score(transcript[-k:], true_items=true_texts, wrong_items=planted.wrong_texts),
        "raw_replay": _score(transcript, true_items=true_texts, wrong_items=planted.wrong_texts),
    }
    return {
        "scenario": scenario,
        "fact": fact.key,
        "k": k,
        "ncp_chunk_ids": ncp_ids,
        "ncp_suppressed_writes": planted.suppressed_writes,
        "results": results,
    }


def _rates(rows: list[dict[str, object]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for arm in ARMS:
        out[arm] = {
            metric: round(sum(1 for row in rows if row["results"][arm][metric]) / len(rows), 4)  # type: ignore[index]
            for metric in METRICS
        }
    return out


def conflict_trust(
    *,
    workdir: str | Path,
    k: int = 1,
    seed: int = 7,
    distractors: int = 4,
    fillers: int = 6,
    scenarios: tuple[str, ...] = SCENARIOS,
    facts: tuple[Fact, ...] = FACTS,
) -> dict[str, object]:
    if k < 1:
        raise ValueError("k must be >= 1")
    if distractors < 1:
        raise ValueError("distractors must be >= 1")
    if fillers < 0:
        raise ValueError("fillers must be >= 0")
    unknown = [name for name in scenarios if name not in SCENARIOS]
    if unknown:
        raise ValueError(f"unknown scenarios: {unknown}")

    base = Path(workdir)
    base.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for scenario in scenarios:
        for fact in facts:
            rows.append(
                _run_task(
                    scenario, fact,
                    store_path=base / f"{scenario}_{fact.key}_k{k}.db",
                    k=k, seed=seed, distractors=distractors, fillers=fillers,
                )
            )

    by_scenario = {name: _rates([row for row in rows if row["scenario"] == name]) for name in scenarios}
    resolvable_rows = [row for row in rows if row["scenario"] in RESOLVABLE]
    resolvable = _rates(resolvable_rows) if resolvable_rows else {}

    summary: dict[str, object] = {"by_scenario": by_scenario, "resolvable": resolvable}
    if resolvable:
        ncp_top1 = resolvable["ncp"]["top1_correct"]
        rag_top1 = resolvable["plain_rag"]["top1_correct"]
        summary["ncp_top1_minus_plain_rag_top1"] = round(ncp_top1 - rag_top1, 4)
        summary["ncp_beats_plain_rag_on_resolvable"] = ncp_top1 > rag_top1
    if "no_conflict" in by_scenario:
        summary["control_all_bounded_arms_find_answer"] = all(
            by_scenario["no_conflict"][arm]["answer_present"] == 1.0 for arm in ("ncp", "plain_rag")
        )
    suppressed = sorted({str(row["scenario"]) for row in rows if row["ncp_suppressed_writes"]})
    summary["scenarios_with_suppressed_writes"] = suppressed
    # Tasks where the store's write-time near-duplicate filter refused the true
    # fact because a contradicting claim with the same wording was already
    # stored. NCP cannot retrieve what it never stored.
    summary["tasks_where_ncp_dropped_the_true_fact_at_write"] = sum(
        1 for row in rows if "fact_true" in row["ncp_suppressed_writes"]  # type: ignore[operator]
    )

    return {
        "benchmark": "conflict_trust",
        "budget": {"mode": "chunk_budget", "chunks": k, "token_unit": token_unit()},
        "seed": seed,
        "distractors": distractors,
        "fillers": fillers,
        "tasks": len(rows),
        "facts": len(facts),
        "rows": rows,
        "summary": summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--k", type=int, default=1, help="chunk budget given to every bounded arm")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--distractors", type=int, default=4)
    parser.add_argument("--fillers", type=int, default=6)
    parser.add_argument("--summary-only", action="store_true", help="omit per-task rows")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="ncp-conflict-bench-") as tmpdir:
        artifact = conflict_trust(
            workdir=tmpdir, k=args.k, seed=args.seed, distractors=args.distractors, fillers=args.fillers
        )
    if args.summary_only:
        artifact.pop("rows")
    print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
