"""Backend-neutral breadth-first traversal shared by the SQLite and PostgreSQL stores.

Both stores supply the same three callbacks, so ordering and truncation
semantics are identical by construction (the parity suite pins them):

* ``fetch_edges(frontier)``: every visible edge touching any frontier node in
  the requested direction, ordered by ``(relation_type, relation_id)``. One
  query per hop, not one per node.
* ``load_relations(rows)``: :class:`Relation` objects for the accepted rows,
  with their valid evidence loaded in one query per hop.
* ``check_deadline()``: raises when the caller's wall-clock budget is spent.

Semantics (unchanged from the per-node implementation): frontier nodes are
expanded in discovery order; at each node its edges come in
``(relation_type, relation_id)`` order; an edge is used once; a new node is
admitted only while fewer than ``max_nodes`` nodes are known; expansion stops
at ``max_edges``. ``direction`` filters which edges a node expands: ``out``
follows edges whose source is the node, ``in`` edges whose target is the node,
``both`` either.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any
from uuid import UUID

from ncp.ontology.models import GraphPath, GraphQuery, GraphResult, Relation

Row = Mapping[str, Any]


def _touches(row: Row, node: str, direction: str) -> bool:
    if direction == "out":
        return row["src_entity_id"] == node
    if direction == "in":
        return row["dst_entity_id"] == node
    return row["src_entity_id"] == node or row["dst_entity_id"] == node


def _other(row: Row, node: str, direction: str) -> str:
    if direction == "in":
        return str(row["src_entity_id"])
    return str(row["dst_entity_id"] if row["src_entity_id"] == node else row["src_entity_id"])


def breadth_first(
    request: GraphQuery,
    seeds: list[str],
    *,
    fetch_edges: Callable[[Sequence[str]], list[Row]],
    load_relations: Callable[[Sequence[Row]], dict[str, Relation]],
    check_deadline: Callable[[], None],
) -> GraphResult:
    warnings: list[str] = []
    paths: list[GraphPath] = []
    if len(seeds) > request.max_nodes:
        seeds = seeds[:request.max_nodes]
        warnings.append(f"truncated: seeds exceed max_nodes={request.max_nodes}")
    direction = request.direction
    chains: dict[str, tuple[tuple[UUID, ...], tuple[Relation, ...]]] = {
        seed: ((UUID(seed),), ()) for seed in seeds
    }
    known: set[str] = set(chains)
    seen_edges: set[str] = set()
    frontier = list(seeds)
    truncated = False
    for _ in range(request.max_hops):
        check_deadline()
        rows = fetch_edges(frontier) if frontier else []
        accepted: list[tuple[str, Row, str]] = []
        next_frontier: list[str] = []
        for node in frontier:
            check_deadline()
            for row in rows:
                if not _touches(row, node, direction):
                    continue
                relation_id = str(row["relation_id"])
                if relation_id in seen_edges:
                    continue
                if len(seen_edges) >= request.max_edges:
                    warnings.append(f"truncated: max_edges={request.max_edges} reached")
                    truncated = True
                    break
                other = _other(row, node, direction)
                if other not in known and len(known) >= request.max_nodes:
                    if not any("max_nodes" in item for item in warnings):
                        warnings.append(f"truncated: max_nodes={request.max_nodes} reached")
                    continue
                seen_edges.add(relation_id)
                accepted.append((node, row, other))
                if other not in known:
                    known.add(other)
                    next_frontier.append(other)
            if truncated:
                break
        relations = load_relations([row for _, row, _ in accepted]) if accepted else {}
        for node, row, other in accepted:
            nodes, chain_relations = chains[node]
            chain = (nodes + (UUID(other),), chain_relations + (relations[str(row["relation_id"])],))
            paths.append(GraphPath(nodes=chain[0], relations=chain[1]))
            chains.setdefault(other, chain)
        frontier = next_frontier
        if truncated or not frontier:
            break
    else:
        if frontier and any(
            str(row["relation_id"]) not in seen_edges for row in fetch_edges(frontier)
        ):
            warnings.append(f"truncated: max_hops={request.max_hops} reached")
    return GraphResult(paths=tuple(paths), warnings=tuple(warnings))


__all__ = ["breadth_first"]
