"""CAP-C5: shared bi-temporal visibility filtering for stored chunk rows.

Backend-agnostic: operates on any row-like object that supports both
``"col" in row.keys()`` and ``row["col"]`` -- ``sqlite3.Row`` and the plain
``dict`` rows produced by the pgvector stores both qualify.

Two dimensions are tracked per chunk:

- **Transaction time** (``created_at``): when NCP recorded the row. Fixed at
  first write, never rewritten.
- **Valid time** (``valid_from`` / ``valid_to``): when the fact was/is true
  in the world. Both nullable; unset means "no bound in that direction."

Supersession (``superseded_by``) records that a newer chunk honestly replaced
this one -- the old row is never deleted, only marked, so history stays
queryable via ``as_of``.

Two views:

- **Default (``as_of=None``)**: the currently-valid view. Excludes chunks
  that are superseded (regardless of when) or whose ``valid_to`` has already
  passed "now". This is a pure "is it live right now" check.
- **Point-in-time (``as_of=<epoch seconds>``)**: "what did we believe as of
  that transaction time." A chunk is visible if it was recorded
  (``created_at <= as_of``), was not yet superseded by a chunk itself
  recorded by ``as_of`` (the transaction time supersession took effect is
  the superseding chunk's own ``created_at``, since ``supersede()`` always
  runs transactionally alongside that chunk's write), and was valid
  (``valid_from``/``valid_to``) at that instant.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _field(row: Any, key: str) -> Any:
    """Fetch column ``key`` from a row-like object, tolerating missing columns.

    Pre-migration rows (or defensive test doubles) may lack the bi-temporal
    columns entirely; treat that exactly like a present-but-NULL value.
    """
    keys_fn = getattr(row, "keys", None)
    if callable(keys_fn) and key not in keys_fn():
        return None
    return row[key]


def is_currently_valid(row: Any, *, now: float) -> bool:
    """Default (``as_of=None``) visibility: not superseded, not expired-valid."""
    if _field(row, "superseded_by") is not None:
        return False
    valid_to = _field(row, "valid_to")
    if valid_to is not None and float(valid_to) <= now:
        return False
    return True


def collect_successor_ids(rows: Sequence[Any]) -> set[str]:
    """Distinct ``superseded_by`` chunk_ids referenced by ``rows``.

    Callers use this to fetch just the ``created_at`` of each successor
    chunk (a single follow-up lookup) before calling ``is_valid_as_of``.
    """
    return {
        str(_field(row, "superseded_by"))
        for row in rows
        if _field(row, "superseded_by") is not None
    }


def is_valid_as_of(
    row: Any,
    *,
    as_of: float,
    successor_created_at: Mapping[str, float],
) -> bool:
    """Point-in-time (``as_of``) visibility.

    ``successor_created_at`` maps chunk_id -> created_at for the chunk
    referenced by this row's ``superseded_by`` (if any). If the successor is
    unknown (missing from the mapping), supersession cannot be confirmed to
    have happened by ``as_of``, so the row stays visible -- correctness over
    aggressiveness.
    """
    created_at = float(_field(row, "created_at"))
    if created_at > as_of:
        return False
    superseded_by = _field(row, "superseded_by")
    if superseded_by is not None:
        successor_ts = successor_created_at.get(str(superseded_by))
        if successor_ts is not None and successor_ts <= as_of:
            return False
    valid_from = _field(row, "valid_from")
    if valid_from is not None and float(valid_from) > as_of:
        return False
    valid_to = _field(row, "valid_to")
    if valid_to is not None and float(valid_to) <= as_of:
        return False
    return True


def filter_bitemporal(
    rows: Sequence[Any],
    *,
    as_of: float | None,
    now: float,
    successor_created_at: Mapping[str, float] | None = None,
) -> list[Any]:
    """Apply CAP-C5 bi-temporal visibility to a batch of already-fetched rows.

    ``successor_created_at`` is only consulted when ``as_of`` is given; pass
    the result of a targeted lookup over ``collect_successor_ids(rows)``
    (empty/omitted is fine when no row in the batch is superseded).
    """
    if as_of is None:
        return [row for row in rows if is_currently_valid(row, now=now)]
    lookup = successor_created_at or {}
    return [row for row in rows if is_valid_as_of(row, as_of=as_of, successor_created_at=lookup)]
