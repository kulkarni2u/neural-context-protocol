#!/usr/bin/env python3
"""Seed a small infrastructure graph into a running ``ncp serve`` over MCP.

Writes one evidence chunk per relationship through ``ncp_write_memory`` (so
the evidence is ordinary NCP memory), registers services, databases, an
external API, a Kafka topic and owning teams with ``ncp_ontology_assert``, and
asserts *verified* relations that cite those chunks. One extra relation is
asserted as ``candidate`` only: it never appears in automatic context.

Deployments: the v1 ``infrastructure`` pack has no Deployment type, so the
deployment facts (environment, version, manifest path) live in the Service
attributes and in the deployment-manifest evidence chunks.

Idempotent: chunk ids, entity keys, idempotency keys and timestamps are fixed,
so running it twice changes nothing. Standard library only.

    python examples/13_ontology/seed_graph.py --url http://127.0.0.1:4242/mcp
"""

from __future__ import annotations

import argparse

from mcp_http import DEFAULT_URL, MCPClient, MCPError

PIPELINE = "demo"
OBSERVED_AT = "2026-01-15T09:00:00+00:00"

ENTITIES = [
    ("Service", "edge-gateway", {"name": "edge-gateway", "environment": "prod",
                                 "version": "5.2.1", "manifest": "deploy/edge-gateway.yaml"}),
    ("Service", "checkout", {"name": "checkout", "environment": "prod",
                             "version": "2.14.0", "manifest": "deploy/checkout.yaml"}),
    ("Service", "payments", {"name": "payments", "environment": "prod",
                             "version": "7.3.2", "manifest": "deploy/payments.yaml"}),
    ("Service", "inventory", {"name": "inventory", "environment": "prod",
                              "version": "1.9.4", "manifest": "deploy/inventory.yaml"}),
    ("Database", "orders-db", {"name": "orders-db", "engine": "postgres"}),
    ("Database", "ledger-db", {"name": "ledger-db", "engine": "postgres"}),
    ("Database", "inventory-db", {"name": "inventory-db", "engine": "mysql"}),
    ("API", "card-network-api", {"name": "card-network-api", "vendor": "external"}),
    ("KafkaTopic", "order-events", {"cluster": "main", "name": "order-events"}),
    ("Team", "team-commerce", {"name": "team-commerce"}),
    ("Team", "team-payments", {"name": "team-payments"}),
]

# (src, relation, dst, evidence chunk text)
RELATIONS = [
    ("edge-gateway", "DEPENDS_ON", "checkout",
     "deploy/edge-gateway.yaml (prod, 5.2.1) routes /api/checkout to the checkout service."),
    ("checkout", "DEPENDS_ON", "payments",
     "deploy/checkout.yaml (prod, 2.14.0) sets PAYMENTS_URL; checkout calls payments to "
     "authorize every order."),
    ("checkout", "DEPENDS_ON", "inventory",
     "checkout reserves stock through the inventory service before confirming an order."),
    ("checkout", "DEPENDS_ON", "orders-db",
     "checkout writes orders to the orders-db Postgres cluster (DATABASE_URL in its manifest)."),
    ("payments", "DEPENDS_ON", "ledger-db",
     "payments records every capture in ledger-db; ledger-db outages stop payments."),
    ("payments", "DEPENDS_ON", "card-network-api",
     "payments calls the external card-network-api for authorization (5s client timeout)."),
    ("inventory", "DEPENDS_ON", "inventory-db",
     "inventory keeps stock levels in inventory-db (MySQL, primary in eu-west-1)."),
    ("checkout", "PRODUCES", "order-events",
     "checkout publishes OrderPlaced to the order-events Kafka topic."),
    ("inventory", "CONSUMES", "order-events",
     "inventory runs consumer group inventory-orders on the order-events topic."),
    ("team-commerce", "OWNS", "edge-gateway", "team-commerce owns edge-gateway (CODEOWNERS)."),
    ("team-commerce", "OWNS", "checkout", "team-commerce owns checkout (CODEOWNERS)."),
    ("team-commerce", "OWNS", "inventory", "team-commerce owns inventory (CODEOWNERS)."),
    ("team-payments", "OWNS", "payments", "team-payments owns payments (CODEOWNERS)."),
]

# Asserted but never verified: excluded from automatic context and from
# verified_only queries.
CANDIDATE = ("edge-gateway", "DEPENDS_ON", "payments",
             "Unconfirmed agent note: edge-gateway may call payments directly for refunds.")


def _chunk_id(src: str, relation: str, dst: str) -> str:
    return f"demo-ev-{src}-{relation.lower()}-{dst}"


def seed(client: MCPClient, pipeline: str = PIPELINE) -> dict[str, object]:
    entity_ids: dict[str, str] = {}
    for entity_type, key, attributes in ENTITIES:
        result = client.call("ncp_ontology_assert", {
            "kind": "entity", "pipeline_id": pipeline,
            "entity": {"entity_type": entity_type, "canonical_key": key, "display_name": key,
                       "attributes": attributes},
        })
        entity_ids[key] = result["entity"]["entity_id"]

    relation_ids: list[str] = []
    for (src, relation, dst, text), status in (
        *((item, "verified") for item in RELATIONS), (CANDIDATE, "candidate"),
    ):
        chunk_id = _chunk_id(src, relation, dst)
        client.call("ncp_write_memory", {
            "chunk_id": chunk_id, "content": text, "layer": "semantic", "src": "tool_result",
            "written_by": "ontology_seeder", "pipeline_id": pipeline,
        })
        result = client.call("ncp_ontology_assert", {
            "kind": "relation", "pipeline_id": pipeline,
            "idempotency_key": f"{src}-{relation}-{dst}",
            "relation": {
                "src_entity_id": entity_ids[src], "dst_entity_id": entity_ids[dst],
                "relation_type": relation, "status": status, "confidence": 0.95,
                "observed_at": OBSERVED_AT,
                "evidence": [{"evidence_type": "chunk", "evidence_id": chunk_id,
                              "source_revision": "1", "observed_at": OBSERVED_AT}],
            },
        })
        relation_ids.append(result["relation"]["relation_id"])
    return {"entities": len(entity_ids), "relations": len(relation_ids),
            "verified": len(RELATIONS), "candidate": 1}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL, help="ncp serve MCP endpoint")
    parser.add_argument("--pipeline", default=PIPELINE, help="pipeline_id (ontology namespace)")
    args = parser.parse_args(argv)
    client = MCPClient(args.url)
    try:
        enabled = bool(client.call("ncp_ontology_capabilities").get("enabled"))
    except MCPError:  # tool not listed: the ontology is off (or the core tool profile)
        enabled = False
    if not enabled:
        print("The ontology is disabled on this server: add the [ontology] section from "
              "examples/13_ontology/config.toml to .ncp/config.toml and restart ncp serve.")
        return 1
    summary = seed(client, args.pipeline)
    print(f"seeded pipeline {args.pipeline!r}: {summary['entities']} entities, "
          f"{summary['verified']} verified relations, {summary['candidate']} candidate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
