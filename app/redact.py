"""Deterministic leaf-path redaction engine for ``POST /redact``.

A rule is ``{"pattern": ..., "action": ..., "value"?: ...}`` where ``pattern``
is an RFC 6901 fragment extended with two wildcard reference tokens:

* a literal token matches that exact token (with the usual ``~0`` / ``~1``
  unescaping, so ``/a~1b`` addresses the member named ``a/b``);
* ``*`` matches exactly one reference token (any object member name or array
  index);
* ``**`` matches zero or more reference tokens (e.g. ``/a/**`` matches the
  leaf ``/a`` itself as well as every descendant leaf).

The token is classified *after* RFC 6901 unescaping, so a member literally
named ``*`` or ``**`` cannot be addressed by a wildcard pattern (it is still
reachable with an exact pointer on the other endpoints).

Targets are leaves in exactly the same sense as everywhere else in the
service (see :mod:`app.tree`): scalars, ``null`` and empty ``{}`` / ``[]``
are leaves, so empty containers can be masked or dropped too.  Matched
leaves form an antichain — a non-empty container is never itself a leaf —
so redactions never invalidate one another's paths.

Actions:

* ``mask`` replaces the leaf with the rule's constant ``value`` (required,
  any JSON value, deep-copied per occurrence);
* ``drop`` removes an object member or sets an array element to ``null``
  (indices are kept stable); dropping the root leaf yields ``null``.

When several rules match one leaf the winner is deterministic: the rule
with more literal tokens wins; on a tie the rule with more ``*`` tokens
wins (equivalently fewer ``**`` tokens); on a further tie the earlier rule
in the submitted array wins.

Every rule envelope is parsed and validated before the document is
touched, so a malformed pattern never produces a partially redacted
result.  Failures raise :class:`~app.errors.ApiError` located at
``rules[i]`` (or one of its fields), matching the other endpoints.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from . import pointers
from .errors import ApiError, INVALID_REQUEST, POINTER_SYNTAX
from .tree import expand_leaves
from .validation import validate_json_value

SUPPORTED_ACTIONS = ("mask", "drop")

# Wildcard token sentinels; every other pattern token is a literal string.
_ONE_LEVEL = object()
_ANY_LEVELS = object()


@dataclass(frozen=True)
class RedactionRule:
    """One validated redaction rule with its precomputed specificity."""

    index: int
    pattern: str
    tokens: Tuple[Any, ...]
    action: str
    value: Any
    literal_count: int
    star_count: int

    def specificity_key(self) -> Tuple[int, int, int]:
        # Lower is better: more literals, then more single stars, then the
        # earlier rule.  ``**`` count never enters the key explicitly — with
        # literal/star counts equal, rule order is the documented tie-break.
        return (-self.literal_count, -self.star_count, self.index)


def _classify_token(token: str) -> Any:
    if token == "*":
        return _ONE_LEVEL
    if token == "**":
        return _ANY_LEVELS
    return token


def _parse_rule(raw: Any, index: int) -> RedactionRule:
    """Structurally validate one rule envelope (no document access)."""

    loc = f"rules[{index}]"
    if not isinstance(raw, dict):
        raise ApiError(
            INVALID_REQUEST,
            "rule must be an object {pattern, action[, value]}, got "
            f"{type(raw).__name__}",
            loc=loc,
        )

    if "pattern" not in raw:
        raise ApiError(
            INVALID_REQUEST, "missing required field 'pattern'", loc=f"{loc}.pattern"
        )
    pattern = raw["pattern"]
    if not isinstance(pattern, str):
        raise ApiError(
            INVALID_REQUEST,
            f"'pattern' must be an RFC 6901 pointer string, got "
            f"{type(pattern).__name__}",
            loc=f"{loc}.pattern",
        )
    try:
        parsed_tokens = pointers.parse_pointer(pattern)
    except pointers.PointerError as exc:
        raise ApiError(
            POINTER_SYNTAX,
            str(exc),
            loc=f"{loc}.pattern",
            detail={"pattern": pattern},
        ) from exc
    tokens = tuple(_classify_token(token) for token in parsed_tokens)

    if "action" not in raw:
        raise ApiError(
            INVALID_REQUEST, "missing required field 'action'", loc=f"{loc}.action"
        )
    action = raw["action"]
    if not isinstance(action, str):
        raise ApiError(
            INVALID_REQUEST,
            f"'action' must be a string, got {type(action).__name__}",
            loc=f"{loc}.action",
        )
    if action not in SUPPORTED_ACTIONS:
        raise ApiError(
            INVALID_REQUEST,
            f"unsupported action {action!r}: only "
            f"{', '.join(SUPPORTED_ACTIONS)} are supported",
            loc=f"{loc}.action",
        )

    value: Any = None
    if action == "mask":
        if "value" not in raw:
            raise ApiError(
                INVALID_REQUEST,
                "a mask rule requires a 'value' field with the replacement "
                "constant",
                loc=f"{loc}.value",
            )
        value = raw["value"]
        validate_json_value(value, loc=f"{loc}.value")

    literal_count = sum(
        1 for token in tokens if token not in (_ONE_LEVEL, _ANY_LEVELS)
    )
    star_count = sum(1 for token in tokens if token is _ONE_LEVEL)
    return RedactionRule(
        index=index,
        pattern=pattern,
        tokens=tokens,
        action=action,
        value=value,
        literal_count=literal_count,
        star_count=star_count,
    )


def pattern_matches(
    pattern_tokens: Tuple[Any, ...], leaf_tokens: Tuple[str, ...]
) -> bool:
    """Match wildcard pattern tokens against concrete leaf pointer tokens.

    Literal tokens must be equal; ``*`` consumes exactly one token; ``**``
    consumes zero or more tokens.  Memoized recursion keeps the backtracking
    linear-ish for realistically shallow documents.
    """

    p_len = len(pattern_tokens)
    l_len = len(leaf_tokens)
    memo: Dict[Tuple[int, int], bool] = {}

    def match(pi: int, li: int) -> bool:
        if pi == p_len:
            return li == l_len
        key = (pi, li)
        cached = memo.get(key)
        if cached is not None:
            return cached
        token = pattern_tokens[pi]
        if token is _ANY_LEVELS:
            result = any(match(pi + 1, k) for k in range(li, l_len + 1))
        elif li == l_len:
            result = False
        elif token is _ONE_LEVEL or token == leaf_tokens[li]:
            result = match(pi + 1, li + 1)
        else:
            result = False
        memo[key] = result
        return result

    return match(0, 0)


def _apply_to_redacted(
    redacted: Any, leaf_tokens: Tuple[str, ...], rule: RedactionRule
) -> Any:
    """Apply one winning rule to the redacted copy; returns the document.

    Matched leaves are an antichain, so every parent path resolved here is
    still present and no array index has shifted (drops null elements rather
    than removing them).
    """

    if len(leaf_tokens) == 0:
        if rule.action == "mask":
            return copy.deepcopy(rule.value)
        # Dropping the root leaf leaves no parent container to delete from.
        return None

    parent = pointers.resolve(redacted, leaf_tokens[:-1])
    last = leaf_tokens[-1]

    if isinstance(parent, dict):
        key = str(last)
        if rule.action == "mask":
            parent[key] = copy.deepcopy(rule.value)
        else:
            del parent[key]
        return redacted

    # Array parent: drop nulls the element instead of shifting later indices.
    idx = pointers.decode_array_index(str(last))
    if rule.action == "mask":
        parent[idx] = copy.deepcopy(rule.value)
    else:
        parent[idx] = None
    return redacted


def apply_redaction(document: Any, raw_rules: Any) -> Dict[str, Any]:
    """Validate *raw_rules* and apply deterministic redactions to a deep copy.

    Returns ``{"document": redacted, "applied": [...]}`` where each applied
    entry is ``{path, action, rule}`` (``rule`` is the zero-based index of the
    winning rule) and the list is sorted by RFC 6901 pointer.  An empty rule
    list is a valid no-op; patterns matching no leaf simply do nothing.
    """

    if not isinstance(raw_rules, list):
        raise ApiError(
            INVALID_REQUEST,
            "'rules' must be an array of {pattern, action[, value]} objects",
            loc="rules",
        )

    # Pass 1: all envelopes (types, action names, pointer syntax, required
    # values) are validated before the document is touched.
    rules: List[RedactionRule] = [
        _parse_rule(raw, i) for i, raw in enumerate(raw_rules)
    ]

    # Pass 2: select a winner per leaf of the original document, then apply
    # the winners to a private copy.  The antichain property guarantees the
    # applications are order-independent.
    redacted = copy.deepcopy(document)
    applied: List[Dict[str, Any]] = []
    for leaf in expand_leaves(document):
        leaf_tokens = tuple(pointers.parse_pointer(leaf.pointer))
        winners = [
            rule
            for rule in rules
            if pattern_matches(rule.tokens, leaf_tokens)
        ]
        if not winners:
            continue
        winner = min(winners, key=RedactionRule.specificity_key)
        applied.append(
            {"path": leaf.pointer, "action": winner.action, "rule": winner.index}
        )
        redacted = _apply_to_redacted(redacted, leaf_tokens, winner)

    applied.sort(key=lambda entry: entry["path"])
    return {"document": redacted, "applied": applied}
