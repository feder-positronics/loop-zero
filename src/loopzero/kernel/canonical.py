"""Canonical persisted-record digest, extracted from the finding ledger."""
import hashlib
import json
from collections.abc import Mapping

class LedgerConflict(ValueError):
    """The persisted payload cannot be represented as canonical JSON."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise LedgerConflict(f"ledger payload is not JSON-serializable: {exc}") from exc


def canonical_record_digest(record: Mapping[str, object]) -> str:
    """Hash every persisted field of a latest record using canonical JSON."""
    return hashlib.sha256(_canonical_json(dict(record)).encode("utf-8")).hexdigest()
