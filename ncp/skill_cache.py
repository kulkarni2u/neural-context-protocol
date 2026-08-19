"""Content-addressed caching for third-party skill/reference content (CAP-C9 v1).

See docs/NCP_SKILL_CACHING_DESIGN.md for the full design and the rationale
for reusing the existing chunk/trust/retrieval machinery rather than adding a
new store table or MCP tool. Scoped for library-API consumers (``ncp.api``)
with no host-level progressive disclosure of their own -- e.g. a custom
subagent harness where every subagent invocation is a fresh context and
nothing else de-duplicates repeated skill loads across them.

Two retrieval shapes, matching two different skill uses:

- **Workflow/protocol skills** (every subagent must follow an identical
  procedure): ``fetch_skill()`` returns the full cached content verbatim, in
  section order, bypassing the assembler's scored retrieval/budget path
  entirely. Partial, relevance-ranked retrieval of a shared protocol would
  let different subagents see different slices of it -- a drift source in
  its own right, which defeats the point for this kind of skill.
- **Review/checklist skills** (a subagent only needs the rule that applies to
  what it's looking at): call ``recall_skills(query)`` alongside your normal
  ``ncp.get_context()`` call and fold the results in. This is a deliberately
  separate call, not automatic inclusion in ``get_context()``'s own
  retrieval: cached skill content lives in ``zone="proven"`` (so it survives
  ordinary working-set GC -- see ``cache_skill()``), and the assembler's
  per-turn retrieval only ever queries ``zone="working"``
  (``ncp/assembler.py`` -- true for ``ncp_fetch`` too, not just
  ``ncp_get_context``). Persistence and automatic-inclusion-in-every-turn
  are in tension for this content: a cached skill should outlive the normal
  working-set churn of a single pipeline, which is exactly what
  ``zone="proven"`` buys, at the cost of one explicit extra call to reach it.

Content-addressing: chunk ids are derived from ``skill_id`` + a hash of the
content, so caching identical content twice (two subagents, two pipelines)
collapses to the same rows via the store's existing upsert-on-conflict write
path -- true cross-agent, cross-pipeline reuse with no explicit invalidation
call needed. Staleness detection is the caller's job (see the design doc
§1.1): the caller already has the skill file open to call ``cache_skill()``
in the first place, so it is best placed to know when the content changed.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from ncp.chunker import chunk_content
from ncp.config import NCPConfig, load_config
from ncp.stores.base import BaseStore
from ncp.stores.factory import create_store
from ncp.types import ChunkLayer, SubconsciousChunk

# SubconsciousChunk.content is hard-capped at 2000 chars (ncp/types.py). Keep
# windows comfortably under that even in the rare case chunk_prose's
# sentence/word windowing slightly overshoots max_tokens*4 chars.
_HARD_CONTENT_CEILING = 2000

# Reserved section index for the manifest chunk (real sections start at 0),
# so fetch_skill() can locate it without a separate store query capability.
_MANIFEST_SECTION_INDEX = -1


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def _section_chunk_id(skill_id: str, content_hash: str, index: int) -> str:
    raw = f"{skill_id}|{content_hash}|{index}"
    return f"skl_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


def _hard_split(text: str, *, limit: int) -> list[str]:
    """Defensive fallback: split on plain char boundaries if a window is still oversized."""

    return [text[i : i + limit] for i in range(0, len(text), limit)] or [text]


@dataclass(slots=True)
class SkillCacheResult:
    """Outcome of one ``cache_skill()`` call."""

    skill_id: str
    content_hash: str
    section_count: int
    chunk_ids: list[str] = field(default_factory=list)
    written: int = 0  # chunks actually inserted/updated (vs. skipped as near-duplicates)


def _skill_author(skill_id: str) -> str:
    """Deterministic synthetic author for every write of one skill's cache.

    ``BaseStore.write()`` rejects re-using an existing ``chunk_id`` with a
    different ``written_by`` (``_assert_src_immutable`` -- a deliberate
    security fix so no caller can silently overwrite another author's
    content in place). Since ``cache_skill()`` gives identical content the
    same content-derived ``chunk_id`` regardless of which subagent happens
    to call it, ``written_by`` must be equally caller-independent, or the
    second subagent's cache attempt would raise instead of reusing the
    cache. Namespaced by ``skill_id`` (not one global constant) so per-skill
    write history stays distinguishable in the store, matching the
    ``ncp:refine`` / ``ncp:refine:rollback`` synthetic-author convention
    ``ncp/refine.py`` already uses for machine-authored bus content.
    """

    return f"ncp:skill_cache:{skill_id}"


def cache_skill(
    content: str,
    *,
    skill_id: str,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    pipeline_id: str | None = None,
    layer: ChunkLayer = "procedural",
    base_trust: float | None = None,
    source_ref: str | None = None,
) -> SkillCacheResult:
    """Cache third-party skill/reference content as content-addressed chunks.

    ``skill_id`` should be namespaced by the caller (e.g. ``"acme/deploy-api"``,
    not just ``"deploy-api"``) to avoid two unrelated skills colliding on the
    same cache key -- NCP has no skill registry and does not enforce this.

    Every write for a given ``skill_id`` uses the same synthetic author (see
    ``_skill_author``) regardless of which subagent/process calls this
    function -- callers don't pass ``written_by`` here, unlike
    ``ncp_write_memory``'s normal per-agent provenance, because this is
    infrastructure content the bus caches on behalf of whichever subagent
    happened to load it first, not any one subagent's own authored work.

    Re-caching identical content is a cheap no-op: the derived chunk ids are
    unchanged, so the store's upsert-on-conflict write collapses to updating
    the same rows. Re-caching *changed* content produces a new set of chunk
    ids (the hash changed), so old and new sections briefly coexist under
    different ids until the old ones age out via ``expiry``.
    """

    resolved_config = config or load_config()
    resolved_store = store or create_store(resolved_config)
    trust = base_trust if base_trust is not None else resolved_config.skill_cache_default_trust
    window_chars = resolved_config.skill_cache_window_chars
    expiry = time.time() + resolved_config.skill_cache_default_expiry_days * 86400
    written_by = _skill_author(skill_id)

    content_hash = _content_hash(content)
    windows = chunk_content(content, chunk_type="prose", max_tokens=max(1, window_chars // 4))
    sections: list[str] = []
    for window in windows:
        if len(window) <= _HARD_CONTENT_CEILING:
            sections.append(window)
        else:
            sections.extend(_hard_split(window, limit=_HARD_CONTENT_CEILING))

    source_refs = [source_ref] if source_ref else []
    written = 0
    chunk_ids: list[str] = []

    for index, section_text in enumerate(sections):
        chunk_id = _section_chunk_id(skill_id, content_hash, index)
        chunk_ids.append(chunk_id)
        chunk = SubconsciousChunk(
            chunk_id=chunk_id,
            layer=layer,
            content=section_text,
            src="skill_ref",
            written_by=written_by,
            base_trust=trust,
            conditions=["skill_ref", f"skill:{skill_id}", f"hash:{content_hash[:16]}", f"section:{index}"],
            source_refs=source_refs,
            pipeline_id=pipeline_id,
            scope="global",
            zone="proven",
            expiry=expiry,
        )
        # allow_duplicate=True: the store's near-duplicate detector
        # (_is_duplicate) exists to catch accidental re-derivation of the
        # same fact under a *different* chunk_id; it is not relevant here.
        # Exact re-caching is already handled correctly by the
        # content-derived chunk_id + upsert-on-conflict write path, and
        # skipping this check also avoids a false-positive drop between two
        # legitimately different-but-similar-looking sections of the same
        # skill (see docs/NCP_SKILL_CACHING_DESIGN.md §5, open question 4).
        if resolved_store.write(chunk, allow_duplicate=True):
            written += 1

    manifest_id = _section_chunk_id(skill_id, content_hash, _MANIFEST_SECTION_INDEX)
    manifest = SubconsciousChunk(
        chunk_id=manifest_id,
        layer=layer,
        # A manifest exists purely so fetch_skill() can discover section_count
        # from the store without a bespoke tag-query store method -- see the
        # module docstring. Not intended to be independently retrieved.
        content=f"skill_ref manifest skill:{skill_id} hash:{content_hash[:16]} sections:{len(sections)}",
        src="skill_ref",
        written_by=written_by,
        base_trust=trust,
        conditions=["skill_ref", f"skill:{skill_id}", f"hash:{content_hash[:16]}", "manifest"],
        source_refs=source_refs,
        pipeline_id=pipeline_id,
        scope="global",
        zone="proven",
        expiry=expiry,
    )
    if resolved_store.write(manifest, allow_duplicate=True):
        written += 1

    return SkillCacheResult(
        skill_id=skill_id,
        content_hash=content_hash,
        section_count=len(sections),
        chunk_ids=chunk_ids,
        written=written,
    )


def fetch_skill(
    skill_id: str,
    *,
    content_hash: str,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> str | None:
    """Return the full cached content for ``skill_id`` at ``content_hash``, verbatim, in order.

    For workflow/protocol skills where every subagent must see the identical
    content -- not a relevance-ranked slice of it. Returns ``None`` if the
    manifest chunk (or any section it names) isn't cached, e.g. this exact
    ``content_hash`` was never written via ``cache_skill()``. Callers compute
    ``content_hash`` the same way ``cache_skill()`` does (``sha256`` of the
    stripped content) so a changed skill file naturally misses here instead
    of silently serving stale content.

    Review/checklist skills should use ``recall_skills()`` instead -- this
    function returns everything for one exact ``skill_id``/``content_hash``,
    with no relevance ranking.
    """

    resolved_config = config or load_config()
    resolved_store = store or create_store(resolved_config)

    manifest_id = _section_chunk_id(skill_id, content_hash, _MANIFEST_SECTION_INDEX)
    manifest_rows = resolved_store.get_chunks_by_ids([manifest_id])
    if not manifest_rows:
        return None

    section_count = _parse_section_count(manifest_rows[0].content)
    if section_count is None:
        return None

    section_ids = [_section_chunk_id(skill_id, content_hash, i) for i in range(section_count)]
    rows_by_id = {chunk.chunk_id: chunk for chunk in resolved_store.get_chunks_by_ids(section_ids)}
    ordered = [rows_by_id[cid] for cid in section_ids if cid in rows_by_id]
    if len(ordered) != section_count:
        # A section chunk aged out (expiry) or was otherwise removed since the
        # manifest was written -- the cached copy is incomplete. Report a
        # miss rather than silently returning a truncated protocol.
        return None

    return "\n\n".join(chunk.content for chunk in ordered)


def recall_skills(
    query: str,
    *,
    k: int = 4,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> list[SubconsciousChunk]:
    """Relevance-ranked retrieval over cached skill/reference content.

    For review/checklist skills, where a subagent only needs the section
    relevant to what it's looking at -- call this alongside your normal
    ``ncp.get_context()`` call and fold the results into the subagent's
    prompt. Not automatic inside ``get_context()`` itself; see the module
    docstring for why (``zone="proven"`` vs. the assembler's
    ``zone="working"`` retrieval).

    Queries ``zone="proven"``/``scope="global"`` (where ``cache_skill()``
    writes) and filters to ``src="skill_ref"`` client-side, since
    ``BaseStore.query()`` has no ``src`` filter parameter. This can also
    surface manifest chunks incidentally if their content happens to score
    -- callers that need to exclude them can check
    ``"manifest" in chunk.conditions``.
    """

    resolved_config = config or load_config()
    resolved_store = store or create_store(resolved_config)
    hits = resolved_store.query(query, k=k, scope="global", zone="proven")
    return [chunk for chunk in hits if chunk.src == "skill_ref"]


def _parse_section_count(manifest_content: str) -> int | None:
    marker = "sections:"
    idx = manifest_content.rfind(marker)
    if idx == -1:
        return None
    tail = manifest_content[idx + len(marker) :].strip()
    digits = "".join(ch for ch in tail if ch.isdigit())
    return int(digits) if digits else None
