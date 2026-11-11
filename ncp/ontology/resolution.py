"""Deterministic entity resolution for automatic ontology enrichment (design §6.2 step 3).

Text from the request (task / slot / intent / query) is split into candidate
terms: word n-grams joined by space, ``-`` and ``_`` (so the slug
``fix_payroll_events_lag`` reaches the keys ``payroll events`` and
``payroll-events``) plus literal entity UUIDs. Terms are looked up *exactly*
(normalized canonical key first, then alias) inside the caller's readable
scope in one read transaction. A term naming one entity resolves it; a term
naming several stays an ambiguous candidate and is never guessed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from ncp.ontology.models import AuthContext, normalize_key

MAX_TERMS = 256
MAX_NGRAM = 4
MAX_TEXT_CHARS = 4_000
MAX_CANDIDATES = 20

_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE,
)
# Word characters plus the in-key punctuation entity keys commonly use; the
# underscore is a separator (slug style) and is rejoined by the n-gram step.
_WORD = re.compile(r"[^\W_]+(?:[.:/-][^\W_]+)*")

STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from",
    "how", "i", "if", "in", "into", "is", "it", "its", "me", "my", "of", "on", "or",
    "our", "should", "so", "that", "the", "their", "then", "there", "these", "this", "to",
    "was", "we", "were", "what", "when", "where", "which", "who", "why", "will", "with",
    "you", "your",
})


def tokenize(text: str) -> list[str]:
    """Normalized words of ``text`` (NFKC, casefold), in order."""
    return _WORD.findall(normalize_key(text[:MAX_TEXT_CHARS]))


def stem(token: str) -> str:
    """Crude deterministic stem: a 6-character prefix (``consumer``/``consumes``)."""
    return token[:6]


def content_tokens(texts: Iterable[str]) -> list[str]:
    """Non-stopword tokens across ``texts``, de-duplicated in order."""
    seen: dict[str, None] = {}
    for text in texts:
        for token in tokenize(text):
            if token not in STOPWORDS:
                seen.setdefault(token, None)
    return list(seen)


def candidate_terms(texts: Iterable[str]) -> tuple[list[str], list[UUID]]:
    """Lookup terms and literal entity ids found in ``texts``.

    Terms keep first-occurrence order and are capped at :data:`MAX_TERMS`;
    single stopwords and one-character words are not looked up.
    """
    terms: dict[str, None] = {}
    ids: dict[UUID, None] = {}
    for text in texts:
        if not text:
            continue
        for match in _UUID.findall(text[:MAX_TEXT_CHARS]):
            ids.setdefault(UUID(match), None)
        words = tokenize(_UUID.sub(" ", text))
        for start in range(len(words)):
            for size in range(1, MAX_NGRAM + 1):
                gram = words[start:start + size]
                if len(gram) < size:
                    break
                if size == 1:
                    word = gram[0]
                    if len(word) < 2 or word in STOPWORDS:
                        continue
                    terms.setdefault(word, None)
                    continue
                if gram[0] in STOPWORDS or gram[-1] in STOPWORDS:
                    continue
                for joiner in (" ", "-", "_"):
                    terms.setdefault(joiner.join(gram), None)
            if len(terms) >= MAX_TERMS:
                break
        if len(terms) >= MAX_TERMS:
            break
    return list(terms)[:MAX_TERMS], list(ids)


@dataclass(frozen=True, slots=True)
class Resolution:
    """Outcome of resolving request text: resolved seeds and ambiguous terms."""

    # entity_id -> the normalized term (or literal id) that resolved it, in
    # first-resolution order.
    resolved: dict[UUID, str] = field(default_factory=dict)
    # normalized term -> candidate ids (at most MAX_CANDIDATES + 1).
    ambiguous: dict[str, tuple[UUID, ...]] = field(default_factory=dict)

    @property
    def candidate_count(self) -> int:
        """Distinct entities considered: resolved plus ambiguous candidates."""
        ids = set(self.resolved)
        for candidates in self.ambiguous.values():
            ids.update(candidates)
        return len(ids)


def resolve(
    store: object,
    texts: Sequence[str],
    *,
    auth: AuthContext,
    schema_packs: Sequence[str] = (),
    deadline: float | None = None,
) -> Resolution:
    """Resolve ``texts`` against ``store`` (a SQLite or Postgres ontology store).

    A term that is a sub-phrase of a longer resolved term (``payroll`` inside
    ``payroll events``) is not used on its own, so the most specific match
    wins; ambiguity in the shorter term then does not block the longer one.
    """
    terms, literal_ids = candidate_terms(texts)
    resolved: dict[UUID, str] = {}
    ambiguous: dict[str, tuple[UUID, ...]] = {}
    if literal_ids:
        visible = store.get_entities(literal_ids, auth=auth, deadline=deadline)  # type: ignore[attr-defined]
        for entity_id in literal_ids:
            entity = visible.get(str(entity_id))
            if entity is None or entity.deleted_at is not None:
                continue
            if schema_packs and entity.schema_pack not in schema_packs:
                continue
            resolved.setdefault(entity_id, str(entity_id))
    if not terms:
        return Resolution(resolved=resolved, ambiguous=ambiguous)
    matches = store.resolve_terms(  # type: ignore[attr-defined]
        terms, auth=auth, schema_packs=tuple(schema_packs), limit=MAX_CANDIDATES,
        deadline=deadline,
    )
    # Longest (most words) first, then first occurrence: deterministic.
    order = {term: index for index, term in enumerate(terms)}
    matched = sorted(matches, key=lambda key: (-len(re.split(r"[ _-]", key)), order.get(key, 0), key))
    covered: list[set[str]] = []
    for key in matched:
        words = set(re.split(r"[ _-]", key))
        if any(words < longer for longer in covered):
            continue  # a sub-phrase of a more specific match
        ids, _matched_on = matches[key]
        covered.append(words)
        if len(ids) == 1:
            resolved.setdefault(ids[0], key)
        else:
            ambiguous[key] = ids
    # An entity both resolved elsewhere and listed as a candidate is resolved.
    for key, ids in list(ambiguous.items()):
        if len(set(ids) - set(resolved)) == 0:
            del ambiguous[key]
    return Resolution(resolved=resolved, ambiguous=ambiguous)


__all__ = [
    "MAX_CANDIDATES", "MAX_TERMS", "Resolution", "STOPWORDS", "candidate_terms",
    "content_tokens", "resolve", "stem", "tokenize",
]
