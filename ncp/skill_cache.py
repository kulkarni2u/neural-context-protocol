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

Typical usage in an orchestrator/DAG harness: the main session calls
``ensure_cached(content, skill_id=...)`` once per skill/reference file at
classification time (skips the write entirely when already fully cached at
that content hash), then threads the returned ``content_hash`` through the
DAG payload to every subagent it spawns. Each subagent -- regardless of
provider or process -- calls ``fetch_skill(skill_id, content_hash=...)``
using that pinned hash, guaranteeing byte-identical content across a
heterogeneous fleet of subagents, not just "whatever's on disk right now."
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Literal

from ncp.config import NCPConfig, load_config
from ncp.stores.base import BaseStore
from ncp.stores.factory import create_store
from ncp.types import ChunkLayer, SubconsciousChunk

# SubconsciousChunk.content is hard-capped at 2000 chars (ncp/types.py).
# Never let a configured window_chars exceed this, regardless of config.
_HARD_CONTENT_CEILING = 2000

# Reserved section index for the manifest chunk (real sections start at 0),
# so fetch_skill() can locate it without a separate store query capability.
_MANIFEST_SECTION_INDEX = -1


# Reserved identity namespace. Every chunk this module writes uses a
# ``written_by`` starting with ``SKILL_AUTHOR_PREFIX`` and a chunk id starting
# with ``SKILL_CHUNK_ID_PREFIX``. Agent-facing write paths (the MCP
# ``ncp_write_memory``) should refuse writes that use either, via
# ``is_reserved_skill_identity()``, so an agent cannot forge a registered
# (user-verified) skill version or overwrite a cached one.
SKILL_AUTHOR_PREFIX = "ncp:skill_cache:"
SKILL_CHUNK_ID_PREFIX = "skl_"

SkillKind = Literal["workflow", "reference"]
_KINDS = ("workflow", "reference")
_REGISTERED_BY_USER = "user"
_USER_REGISTERED_TRUST = 0.95  # same level as src="user_verified" elsewhere

_SKILL_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
_LIST_PAGE = 200
_LIST_MAX_PAGES = 50


def is_reserved_skill_identity(*, written_by: str | None = None, chunk_id: str | None = None) -> bool:
    """True if ``written_by`` or ``chunk_id`` lies in this module's reserved namespace."""

    if written_by and written_by.startswith(SKILL_AUTHOR_PREFIX):
        return True
    return bool(chunk_id and chunk_id.startswith(SKILL_CHUNK_ID_PREFIX))


def _validate_skill_id(skill_id: str) -> None:
    if not isinstance(skill_id, str) or not _SKILL_ID_RE.match(skill_id):
        raise ValueError(
            f"invalid skill_id {skill_id!r}: expected '<namespace>/<name>' "
            "(letters, digits, '_', '-', '.' in each part)"
        )


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def _section_chunk_id(skill_id: str, content_hash: str, index: int, *, pipeline_id: str | None = None) -> str:
    # ``pipeline_id`` salts the id for pipeline-scoped caches so a pipeline's
    # private copy never collides with (or overwrites) the global one. Global
    # ids (pipeline_id=None) are unchanged from earlier versions.
    raw = f"{skill_id}|{content_hash}|{index}"
    if pipeline_id:
        raw += f"|{pipeline_id}"
    return f"{SKILL_CHUNK_ID_PREFIX}{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


def _split_verbatim(content: str, *, limit: int) -> list[str]:
    """Split into <=``limit``-char windows that concatenate back to ``content`` exactly.

    Deliberately *not* ``ncp.chunker.chunk_content()`` -- that's the
    retrieval chunker, built for scoring/relevance quality, not lossless
    round-tripping: ``chunk_prose`` splits on sentence boundaries and
    rejoins with single spaces, which silently flattens markdown structure
    (headers glued onto the preceding sentence, bullet lists collapsed onto
    one line). For a skill/reference file -- markdown with headers, code
    fences, and lists, not prose -- that's not a formatting nit, it's data
    loss a subagent reconstructing "the protocol" would never see.

    This function never strips or rejoins anything: it walks ``content``
    left to right, cutting windows at the last paragraph break (``\\n\\n``)
    at or before ``limit`` chars, falling back to the last line break
    (``\\n``), falling back to a hard character cut only when neither
    exists within the window (e.g. one long unbroken line). The delimiter
    characters stay attached to the end of the window they close, so
    ``"".join(_split_verbatim(content, limit=N)) == content`` always, for
    any ``limit >= 1`` -- no separator needs to be reinserted at
    reconstruction time (see ``fetch_skill()``).
    """

    if not content:
        return []
    windows: list[str] = []
    pos = 0
    total = len(content)
    while pos < total:
        remaining = total - pos
        if remaining <= limit:
            windows.append(content[pos:])
            break
        window = content[pos : pos + limit]
        idx = window.rfind("\n\n")
        if idx != -1:
            cut = idx + 2
        else:
            idx = window.rfind("\n")
            cut = idx + 1 if idx != -1 else limit
        windows.append(content[pos : pos + cut])
        pos += cut
    return windows


@dataclass(slots=True)
class SkillCacheResult:
    """Outcome of one ``cache_skill()`` call."""

    skill_id: str
    content_hash: str
    section_count: int
    chunk_ids: list[str] = field(default_factory=list)
    written: int = 0  # chunks actually inserted/updated (vs. skipped as near-duplicates)
    kind: str = "reference"
    registered_by: str | None = None
    scope: str = "global"


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

    return f"{SKILL_AUTHOR_PREFIX}{skill_id}"


def _validate_kind(kind: str) -> None:
    if kind not in _KINDS:
        raise ValueError(f"invalid kind {kind!r}: expected one of {_KINDS}")


def _validate_registered_by(registered_by: str | None) -> None:
    if registered_by not in (None, _REGISTERED_BY_USER):
        raise ValueError(f"invalid registered_by {registered_by!r}: expected 'user' or None")


def _target_scope(pipeline_id: str | None, scope_global: bool) -> tuple[str, str | None]:
    """Return ``(chunk scope, chunk-id salt)`` for a write."""

    if pipeline_id and not scope_global:
        return "pipeline", pipeline_id
    return "global", None


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
    kind: SkillKind = "reference",
    registered_by: str | None = None,
    scope_global: bool = False,
) -> SkillCacheResult:
    """Cache third-party skill/reference content as content-addressed chunks.

    ``skill_id`` must be ``<namespace>/<name>`` (e.g. ``"acme/deploy-api"``;
    letters, digits, ``_``, ``-``, ``.`` in each part); ``ValueError``
    otherwise.

    ``kind`` is ``"workflow"`` (fetched verbatim via ``fetch_skill()``,
    never surfaced by ``recall_skills()``) or ``"reference"`` (default;
    relevance-recallable). ``registered_by="user"`` marks the version as
    user-registered: base_trust seeds at 0.95 (user_verified level) unless an
    explicit ``base_trust`` is passed, and the manifest records
    ``registered_by:user`` (see ``is_registered_skill()``). ``None`` keeps the
    configured ``[skill_cache].default_trust``.

    Scope: when ``pipeline_id`` is given and ``scope_global`` is False
    (default), chunks are cached with ``scope="pipeline"`` for that pipeline
    only. ``scope_global=True`` caches globally. When no ``pipeline_id`` is
    given the chunks are global, as in earlier versions.

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

    _validate_skill_id(skill_id)
    _validate_kind(kind)
    _validate_registered_by(registered_by)
    resolved_config = config or load_config()
    resolved_store = store or create_store(resolved_config)
    if base_trust is not None:
        trust = base_trust
    elif registered_by == _REGISTERED_BY_USER:
        trust = _USER_REGISTERED_TRUST
    else:
        trust = resolved_config.skill_cache_default_trust
    chunk_scope, salt = _target_scope(pipeline_id, scope_global)
    window_chars = resolved_config.skill_cache_window_chars
    expiry = time.time() + resolved_config.skill_cache_default_expiry_days * 86400
    written_by = _skill_author(skill_id)

    content_hash = _content_hash(content)
    limit = min(max(1, window_chars), _HARD_CONTENT_CEILING)
    sections = _split_verbatim(content.strip(), limit=limit)

    source_refs = [source_ref] if source_ref else []
    written = 0
    chunk_ids: list[str] = []

    for index, section_text in enumerate(sections):
        chunk_id = _section_chunk_id(skill_id, content_hash, index, pipeline_id=salt)
        chunk_ids.append(chunk_id)
        chunk = SubconsciousChunk(
            chunk_id=chunk_id,
            layer=layer,
            content=section_text,
            src="skill_ref",
            written_by=written_by,
            base_trust=trust,
            conditions=[
                "skill_ref",
                f"skill:{skill_id}",
                f"hash:{content_hash[:16]}",
                f"section:{index}",
                f"kind:{kind}",
            ],
            source_refs=source_refs,
            pipeline_id=pipeline_id,
            scope=chunk_scope,
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

    manifest_id = _section_chunk_id(skill_id, content_hash, _MANIFEST_SECTION_INDEX, pipeline_id=salt)
    manifest_conditions = ["skill_ref", f"skill:{skill_id}", f"hash:{content_hash[:16]}", "manifest", f"kind:{kind}"]
    if registered_by == _REGISTERED_BY_USER:
        manifest_conditions.append(f"registered_by:{_REGISTERED_BY_USER}")
    manifest = SubconsciousChunk(
        chunk_id=manifest_id,
        layer=layer,
        # A manifest exists purely so fetch_skill() can discover section_count
        # from the store without a bespoke tag-query store method -- see the
        # module docstring. Not intended to be independently retrieved.
        content=(
            f"skill_ref manifest skill:{skill_id} hash:{content_hash[:16]} sections:{len(sections)} "
            f"kind:{kind} registered_by:{registered_by or 'none'} chash:{content_hash}"
        ),
        src="skill_ref",
        written_by=written_by,
        base_trust=trust,
        conditions=manifest_conditions,
        source_refs=source_refs,
        pipeline_id=pipeline_id,
        scope=chunk_scope,
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
        kind=kind,
        registered_by=registered_by,
        scope=chunk_scope,
    )


@dataclass(slots=True)
class EnsureCachedResult:
    """Outcome of one ``ensure_cached()`` call."""

    skill_id: str
    content_hash: str
    cache_hit: bool  # True: already fully cached, no write happened this call.


def ensure_cached(
    content: str,
    *,
    skill_id: str,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    pipeline_id: str | None = None,
    layer: ChunkLayer = "procedural",
    base_trust: float | None = None,
    source_ref: str | None = None,
    kind: SkillKind = "reference",
    registered_by: str | None = None,
    scope_global: bool = False,
) -> EnsureCachedResult:
    """Fetch-or-cache in one call -- the common case at every skill-load site.

    The caller here always already has ``content`` in hand (it just read the
    skill/reference file to call this), so there's nothing to *return* the
    way ``fetch_skill()`` returns content to a caller who doesn't have it.
    What this saves is the section-by-section re-chunk-and-write work on
    every call where nothing changed: it checks whether ``skill_id`` is
    already completely cached at this exact content hash *in the target
    scope* (see ``cache_skill()``), with the same ``kind`` and at least the
    requested provenance, and only calls ``cache_skill()`` on an actual miss
    (first-ever call, content changed, or a user registration of content
    previously cached without it).

    Typical call site: the main/orchestrator session's classification step,
    once per workflow skill or selected reference file, before any subagent
    is spawned. Returns ``content_hash`` so the caller can thread it through
    the DAG/task payload to every downstream subagent, which then calls
    ``fetch_skill(skill_id, content_hash=...)`` themselves -- same hash,
    guaranteed byte-identical content, regardless of which provider or
    process each subagent runs in.
    """

    _validate_skill_id(skill_id)
    _validate_kind(kind)
    _validate_registered_by(registered_by)
    resolved_config = config or load_config()
    resolved_store = store or create_store(resolved_config)
    content_hash = _content_hash(content)
    _, salt = _target_scope(pipeline_id, scope_global)

    found = _find_manifest(skill_id, content_hash, resolved_store, [salt])
    if found is not None:
        info = found[1]
        provenance_ok = registered_by != _REGISTERED_BY_USER or info["registered_by"] == _REGISTERED_BY_USER
        if (
            info["kind"] == kind
            and provenance_ok
            and info["sections"] is not None
            and _load_complete_sections(skill_id, content_hash, info["sections"], resolved_store, salt) is not None
        ):
            return EnsureCachedResult(skill_id=skill_id, content_hash=content_hash, cache_hit=True)

    cache_skill(
        content,
        skill_id=skill_id,
        store=resolved_store,
        config=resolved_config,
        pipeline_id=pipeline_id,
        layer=layer,
        base_trust=base_trust,
        source_ref=source_ref,
        kind=kind,
        registered_by=registered_by,
        scope_global=scope_global,
    )
    return EnsureCachedResult(skill_id=skill_id, content_hash=content_hash, cache_hit=False)


def _parse_manifest(chunk_or_content: SubconsciousChunk | dict) -> dict:
    """Parse the ``key:value`` tokens of a manifest chunk's content."""

    content = chunk_or_content["content"] if isinstance(chunk_or_content, dict) else chunk_or_content.content
    tokens: dict[str, str] = {}
    for token in content.split():
        key, sep, value = token.partition(":")
        if sep:
            tokens[key] = value
    sections = tokens.get("sections")
    registered = tokens.get("registered_by")
    kind = tokens.get("kind")
    return {
        "sections": int(sections) if sections and sections.isdigit() else None,
        "kind": kind if kind in _KINDS else "reference",
        "registered_by": registered if registered == _REGISTERED_BY_USER else None,
        "content_hash": tokens.get("chash"),
    }


def _find_manifest(
    skill_id: str, content_hash: str, store: BaseStore, salts: list[str | None]
) -> tuple[SubconsciousChunk, dict, str | None] | None:
    """Return ``(manifest, parsed, salt)`` for the first salt whose manifest exists."""

    for salt in salts:
        manifest_id = _section_chunk_id(skill_id, content_hash, _MANIFEST_SECTION_INDEX, pipeline_id=salt)
        rows = store.get_chunks_by_ids([manifest_id])
        if rows:
            return rows[0], _parse_manifest(rows[0]), salt
    return None


def _lookup_salts(pipeline_id: str | None) -> list[str | None]:
    # Pipeline-scoped copy first, then the global one.
    return [pipeline_id, None] if pipeline_id else [None]


def _load_complete_sections(
    skill_id: str, content_hash: str, section_count: int, store: BaseStore, salt: str | None = None
) -> list[SubconsciousChunk] | None:
    section_ids = [_section_chunk_id(skill_id, content_hash, i, pipeline_id=salt) for i in range(section_count)]
    rows_by_id = {chunk.chunk_id: chunk for chunk in store.get_chunks_by_ids(section_ids)}
    ordered = [rows_by_id[cid] for cid in section_ids if cid in rows_by_id]
    if len(ordered) != section_count:
        # A section chunk aged out (expiry) or was otherwise removed since the
        # manifest was written -- the cached copy is incomplete.
        return None
    return ordered


@dataclass(slots=True)
class SkillInfo:
    """Manifest-level facts about one cached skill version."""

    skill_id: str
    content_hash: str
    kind: str
    section_count: int
    registered_by: str | None
    created_at: float
    scope: str = "global"
    pipeline_id: str | None = None

    @property
    def sections(self) -> int:
        return self.section_count

    @property
    def age(self) -> float:
        """Seconds since the manifest was written."""
        return max(0.0, time.time() - self.created_at)


def _complete_manifest_rows(skill_id: str | None, store: BaseStore, pipeline_id: str | None) -> list[SkillInfo]:
    """All COMPLETE cached versions visible to ``pipeline_id`` (global + that pipeline), newest first.

    Uses ``BaseStore.list_chunks`` (src/zone filtered, paged) to find
    manifests, then verifies section completeness through
    ``get_chunks_by_ids``. Backends without ``list_chunks`` yield nothing.
    """

    author = _skill_author(skill_id) if skill_id else None
    candidates: list[tuple[dict, dict]] = []
    offset = 0
    for _ in range(_LIST_MAX_PAGES):
        page = store.list_chunks(src="skill_ref", zone="proven", written_by=author, limit=_LIST_PAGE, offset=offset)
        rows = page.get("chunks", [])
        for row in rows:
            if row.get("tombstoned") or not str(row["content"]).startswith("skill_ref manifest "):
                continue
            if not str(row["written_by"]).startswith(SKILL_AUTHOR_PREFIX):
                continue
            if row["scope"] != "global" and not (pipeline_id and row.get("pipeline_id") == pipeline_id):
                continue
            parsed = _parse_manifest(row)
            if parsed["content_hash"] and parsed["sections"] is not None:
                candidates.append((row, parsed))
        offset += _LIST_PAGE
        if len(rows) < _LIST_PAGE or offset >= int(page.get("total", 0)):
            break

    out: list[SkillInfo] = []
    for row, parsed in candidates:
        sid = str(row["written_by"])[len(SKILL_AUTHOR_PREFIX) :]
        salt = row.get("pipeline_id") if row["scope"] == "pipeline" else None
        if _load_complete_sections(sid, parsed["content_hash"], parsed["sections"], store, salt) is None:
            continue
        out.append(
            SkillInfo(
                skill_id=sid,
                content_hash=parsed["content_hash"],
                kind=parsed["kind"],
                section_count=parsed["sections"],
                registered_by=parsed["registered_by"],
                created_at=float(row["created_at"]),
                scope=str(row["scope"]),
                pipeline_id=row.get("pipeline_id"),
            )
        )
    out.sort(key=lambda info: info.created_at, reverse=True)
    return out


def latest_skill(
    skill_id: str,
    *,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    pipeline_id: str | None = None,
) -> SkillInfo | None:
    """Manifest info for the newest COMPLETE cached version of ``skill_id``, or ``None``.

    Considers global caches plus, if ``pipeline_id`` is given, that
    pipeline's own caches. Versions with a missing/expired section are skipped.
    """

    _validate_skill_id(skill_id)
    resolved_store = store or create_store(config or load_config())
    versions = _complete_manifest_rows(skill_id, resolved_store, pipeline_id)
    return versions[0] if versions else None


def list_skills(
    *,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    pipeline_id: str | None = None,
) -> list[SkillInfo]:
    """One ``SkillInfo`` per ``skill_id`` (its latest complete version), newest first.

    ``SkillInfo`` exposes ``skill_id``, ``content_hash``, ``kind``,
    ``sections`` (alias of ``section_count``), ``registered_by`` and ``age``
    (seconds).
    """

    resolved_store = store or create_store(config or load_config())
    latest: dict[str, SkillInfo] = {}
    for info in _complete_manifest_rows(None, resolved_store, pipeline_id):  # newest first
        latest.setdefault(info.skill_id, info)
    return list(latest.values())


def is_registered_skill(
    skill_id: str,
    content_hash: str,
    *,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    pipeline_id: str | None = None,
) -> bool:
    """True only if this exact version is complete AND was user-registered.

    Checks the manifest is ``src="skill_ref"`` from the reserved author
    namespace (so a forged chunk from another author never counts), records
    ``registered_by:user``, and that every section is still present.
    """

    resolved_store = store or create_store(config or load_config())
    found = _find_manifest(skill_id, content_hash, resolved_store, _lookup_salts(pipeline_id))
    if found is None:
        return False
    manifest, info, salt = found
    if manifest.src != "skill_ref" or manifest.written_by != _skill_author(skill_id):
        return False
    if info["registered_by"] != _REGISTERED_BY_USER or info["sections"] is None:
        return False
    return _load_complete_sections(skill_id, content_hash, info["sections"], resolved_store, salt) is not None


def fetch_skill(
    skill_id: str,
    *,
    content_hash: str | None = None,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    pipeline_id: str | None = None,
) -> str | None:
    """Return the full cached content for ``skill_id`` at ``content_hash``, verbatim, in order.

    ``content_hash=None`` means the latest complete cached version (see
    ``latest_skill()``). ``pipeline_id`` additionally makes that pipeline's
    own scoped caches visible (looked up before global ones).

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

    if content_hash is None:
        _validate_skill_id(skill_id)
        versions = _complete_manifest_rows(skill_id, resolved_store, pipeline_id)
        if not versions:
            return None
        content_hash = versions[0].content_hash
        salts = [versions[0].pipeline_id if versions[0].scope == "pipeline" else None]
    else:
        salts = _lookup_salts(pipeline_id)

    found = _find_manifest(skill_id, content_hash, resolved_store, salts)
    if found is None or found[1]["sections"] is None:
        return None
    _, info, salt = found

    ordered = _load_complete_sections(skill_id, content_hash, info["sections"], resolved_store, salt)
    if ordered is None:
        # Report a miss rather than silently returning a truncated protocol.
        return None

    # Plain concatenation, not "\n\n".join(): _split_verbatim() keeps each
    # window's own trailing delimiter characters attached at write time
    # (see cache_skill()), so no separator needs to be reinserted here --
    # doing so would double up whatever whitespace was already at each
    # window boundary.
    return "".join(chunk.content for chunk in ordered)


def recall_skills(
    query: str,
    *,
    k: int = 4,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    pipeline_id: str | None = None,
) -> list[SubconsciousChunk]:
    """Relevance-ranked retrieval over cached *reference* skill content.

    For review/checklist skills, where a subagent only needs the section
    relevant to what it's looking at -- call this alongside your normal
    ``ncp.get_context()`` call and fold the results in. Not automatic inside
    ``get_context()`` itself; see the module docstring for why
    (``zone="proven"`` vs. the assembler's ``zone="working"`` retrieval).

    Workflow skills (``kind="workflow"``) are excluded: they must be fetched
    whole via ``fetch_skill()``, never as a relevance slice. Queries
    ``zone="proven"``/``scope="global"`` (plus, if ``pipeline_id`` is given,
    that pipeline's ``scope="pipeline"`` caches) and filters to
    ``src="skill_ref"`` client-side, since ``BaseStore.query()`` has no
    ``src`` filter parameter. This can also surface manifest chunks
    incidentally if their content happens to score -- callers that need to
    exclude them can check ``"manifest" in chunk.conditions``.
    """

    resolved_config = config or load_config()
    resolved_store = store or create_store(resolved_config)
    hits = list(resolved_store.query(query, k=k, scope="global", zone="proven"))
    if pipeline_id:
        seen = {chunk.chunk_id for chunk in hits}
        for chunk in resolved_store.query(query, k=k, scope="pipeline", pipeline_id=pipeline_id, zone="proven"):
            if chunk.chunk_id not in seen:
                hits.append(chunk)
    return [chunk for chunk in hits if chunk.src == "skill_ref" and "kind:workflow" not in chunk.conditions][:k]
