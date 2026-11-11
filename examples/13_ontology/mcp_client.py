#!/usr/bin/env python3
"""Generic MCP client walkthrough: plain JSON-RPC over HTTP, no NCP imports.

1. ``initialize`` + ``tools/list`` -- ontology tools are discoverable like any
   other MCP tool (hosts that cache tool lists may need a refresh).
2. Automatic mode -- an *unchanged* ``ncp_get_context`` call. When the task
   names a known entity with dependency intent, the returned context carries
   an ``[NCP:ONTOLOGY]`` block of verified, evidence-cited paths.
3. Explicit mode -- ``ncp_ontology_capabilities`` -> ``ncp_ontology_query`` ->
   ``ncp_ontology_explain`` for graph-specific questions.

Graph results are context, not execution authorization.

    python examples/13_ontology/seed_graph.py
    python examples/13_ontology/mcp_client.py --url http://127.0.0.1:4242/mcp
"""

from __future__ import annotations

import argparse
import json

from mcp_http import DEFAULT_URL, MCPClient, MCPError

PIPELINE = "demo"


def _statement(path: dict, entities: dict) -> str:
    """``Service:checkout -DEPENDS_ON-> Service:payments`` from a query path."""
    nodes = path["nodes"]
    parts = []
    for index, relation in enumerate(path["relations"]):
        here, there = nodes[index], nodes[index + 1]
        if index == 0:
            parts.append(_label(entities[here]))
        forward = relation["src_entity_id"] == here
        kind = relation["relation_type"]
        parts.append(f"-{kind}->" if forward else f"<-{kind}-")
        parts.append(_label(entities[there]))
    return " ".join(parts)


def _label(entity: dict) -> str:
    return f"{entity['entity_type']}:{entity['canonical_key']}"


def run(
    client: MCPClient, pipeline: str = PIPELINE, *, key: str = "checkout",
    agent_id: str = "generic_client",
) -> dict:
    report: dict[str, object] = {}

    # 1. discovery
    client.request("initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "generic-mcp-client", "version": "1.0"},
    })
    tools = [tool["name"] for tool in client.request("tools/list")["tools"]]  # type: ignore[index]
    report["ontology_tools"] = sorted(name for name in tools if name.startswith("ncp_ontology_"))
    print("ontology tools:", ", ".join(report["ontology_tools"]) or "(none: ontology disabled)")

    # 2. automatic mode: the same ncp_get_context call any NCP harness already makes
    context = client.call("ncp_get_context", {
        "agent_id": agent_id, "role": "build", "pipeline_id": pipeline,
        "task": f"fix_{key.replace('-', '_')}_latency", "slot": key.replace("-", "_"),
        "intent": "find_upstream_dependencies", "max_tokens": 800,
    })
    meta = context.get("ontology") or {}
    report["auto"] = {
        "triggered": meta.get("ontology_triggered"), "reason_code": meta.get("reason_code"),
        "verified_path_count": meta.get("verified_path_count"),
        "has_ontology_block": "[NCP:ONTOLOGY]" in context["context"],
    }
    print("\n== automatic mode: ncp_get_context ==")
    print(context["context"])
    print("ontology metadata:", json.dumps(report["auto"]))

    # 3. explicit mode
    capabilities = client.call("ncp_ontology_capabilities")
    packs = [f"{pack['id']}.v{pack['version']}" for pack in capabilities.get("packs", [])]
    print("\n== explicit mode ==")
    print("capabilities: packs", packs, "limits", json.dumps(capabilities.get("limits")))
    result = client.call("ncp_ontology_query", {
        "pipeline_id": pipeline, "key": key, "entity_type": "Service", "max_hops": 2,
    })
    if not result.get("resolved"):
        candidates = result["ambiguity"]["candidates"]
        print("ambiguous key; candidates:", [_label(item) for item in candidates])
        report["query"] = {"resolved": False}
        return report
    statements = [_statement(path, result["entities"]) for path in result["paths"]]
    report["query"] = {"resolved": True, "paths": len(statements), "warnings": result["warnings"]}
    print(f"query {key!r}: {len(statements)} verified paths")
    for statement in statements:
        print("  ", statement)

    first = result["paths"][0]["relations"][-1]["relation_id"] if result["paths"] else None
    if first is not None:
        explained = client.call("ncp_ontology_explain", {"pipeline_id": pipeline,
                                                         "relation_id": first})
        report["explain"] = explained["validity"]
        print("explain", first, "->", json.dumps(explained["validity"]))
        for item in explained["evidence"]:
            print("   evidence", item.get("evidence_type"), item.get("evidence_id"),
                  "live" if item.get("live") else "not live")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL, help="ncp serve MCP endpoint")
    parser.add_argument("--pipeline", default=PIPELINE, help="pipeline_id (ontology namespace)")
    parser.add_argument("--key", default="checkout", help="entity key to investigate")
    parser.add_argument(
        "--agent-id", default="generic_client",
        help="agent_id for ncp_get_context; with [principals] it must match the token's principal",
    )
    args = parser.parse_args(argv)
    try:
        run(MCPClient(args.url), args.pipeline, key=args.key, agent_id=args.agent_id)
    except MCPError as exc:
        print("MCP error:", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
