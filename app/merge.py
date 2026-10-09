"""Field schema, op validation, and per-type merge strategies."""

from typing import Any

STATUSES = {"TODO", "IN_PROGRESS", "DONE"}

# field -> merge semantics
SCHEMA = {
    "title": "scalar",
    "body": "scalar",
    "status": "scalar",
    "tags": "set",
    "views": "counter",
}

DEFAULTS: dict[str, Any] = {
    "title": "",
    "body": "",
    "status": "TODO",
    "tags": [],
    "views": 0,
    "deleted": False,
}

MAX_TITLE = 200
MAX_BODY = 10_000
MAX_TAGS = 20
MAX_TAG_LEN = 32
MAX_TAG_OPS = 50  # items per tags.add / tags.remove list
MAX_INCREMENT = 1_000_000
# SQLite INTEGER range (signed 64-bit); larger values raise OverflowError on bind.
SQLITE_INT_MAX = 2**63 - 1
SQLITE_INT_MIN = -(2**63)
# Largest POST body accepted; bigger requests are a 413 before any parsing.
MAX_REQUEST_BYTES = 256 * 1024


def new_defaults() -> dict[str, Any]:
    d = dict(DEFAULTS)
    d["tags"] = []
    return d


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _validate_scalar(field: str, spec: dict) -> str | None:
    if set(spec) != {"set"}:
        return f"'{field}' takes exactly {{\"set\": value}}"
    v = spec["set"]
    if not isinstance(v, str):
        return f"'{field}' must be a string"
    if field == "title" and len(v) > MAX_TITLE:
        return f"'title' exceeds {MAX_TITLE} characters"
    if field == "body" and len(v) > MAX_BODY:
        return f"'body' exceeds {MAX_BODY} characters"
    if field == "status" and v not in STATUSES:
        return f"'status' must be one of {sorted(STATUSES)}"
    return None


def _validate_set(field: str, spec: dict) -> str | None:
    if not spec or not set(spec) <= {"add", "remove"}:
        return f'\'{field}\' takes {{"add": [...], "remove": [...]}}'
    lists = {}
    for key in ("add", "remove"):
        items = spec.get(key, [])
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            return f"'{field}.{key}' must be a list of strings"
        if len(items) > MAX_TAG_OPS:
            return f"'{field}.{key}' has {len(items)} items; at most {MAX_TAG_OPS} per request"
        if any(not i or len(i) > MAX_TAG_LEN for i in items):
            return f"'{field}' items must be 1-{MAX_TAG_LEN} characters"
        lists[key] = set(items)
    if lists["add"] & lists["remove"]:
        return f"'{field}' has the same element in add and remove"
    return None


def _validate_counter(field: str, spec: dict) -> str | None:
    if set(spec) != {"increment"}:
        return f"'{field}' takes exactly {{\"increment\": int}}"
    n = spec["increment"]
    if not _is_int(n) or abs(n) > MAX_INCREMENT:
        return f"'{field}.increment' must be an integer with |n| <= {MAX_INCREMENT}"
    return None


_VALIDATORS = {"scalar": _validate_scalar, "set": _validate_set, "counter": _validate_counter}


def validate_changes(op: str, changes: dict[str, dict]) -> str | None:
    """Return an error message, or None if the changes are valid for this op."""
    if op == "delete":
        return "delete takes no changes" if changes else None
    if op == "update" and not changes:
        return "update needs at least one change"
    for field, spec in changes.items():
        if field not in SCHEMA:
            return f"unknown field '{field}'"
        err = _VALIDATORS[SCHEMA[field]](field, spec)
        if err:
            return err
    return None


def apply_field(field: str, current: Any, spec: dict) -> Any:
    """Apply one validated op to the current value. Raises ValueError on a limit breach."""
    kind = SCHEMA[field]
    if kind == "scalar":
        return spec["set"]
    if kind == "set":
        result = (set(current) | set(spec.get("add", []))) - set(spec.get("remove", []))
        if len(result) > MAX_TAGS:
            raise ValueError(f"'{field}' would exceed {MAX_TAGS} items")
        return sorted(result)
    result = current + spec["increment"]
    if not SQLITE_INT_MIN <= result <= SQLITE_INT_MAX:
        raise ValueError(f"'{field}' would leave the range {SQLITE_INT_MIN}..{SQLITE_INT_MAX}")
    return result
