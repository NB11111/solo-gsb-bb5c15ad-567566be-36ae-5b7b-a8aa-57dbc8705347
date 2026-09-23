"""End-to-end tests for POST /redact (deterministic leaf-path redaction)."""

import copy

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _redact(document, rules):
    return client.post("/redact", json={"document": document, "rules": rules})


DOC = {
    "subject": {
        "id": "u-123",
        "name": "Ada Lovelace",
        "age": 36,
        "isAdult": True,
        "nickname": None,
    },
    "emails": ["ada@example.com", "ada@analytical.engine"],
    "nested": {"deep": {"secret": 7, "other": 8}},
    "emptyObj": {},
    "emptyArr": [],
    "a/b": {"tilde~key": "escaped"},
}


# ---------------------------------------------------------------------------
# Literal patterns
# ---------------------------------------------------------------------------


def test_literal_mask_replaces_value_with_constant():
    resp = _redact(DOC, [{"pattern": "/subject/name", "action": "mask",
                          "value": "***"}])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"]["subject"]["name"] == "***"
    # Everything else is untouched.
    assert body["document"]["subject"]["id"] == "u-123"
    assert body["document"]["emails"] == DOC["emails"]
    assert body["applied"] == [
        {"path": "/subject/name", "action": "mask", "rule": 0}
    ]


def test_literal_drop_removes_object_member():
    resp = _redact({"a": 1, "b": 2}, [{"pattern": "/a", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"b": 2}
    assert resp.json()["applied"] == [
        {"path": "/a", "action": "drop", "rule": 0}
    ]


def test_literal_drop_nulls_array_element_without_shifting():
    resp = _redact({"xs": ["a", "b", "c"]},
                   [{"pattern": "/xs/1", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"] == {"xs": ["a", None, "c"]}
    assert len(body["document"]["xs"]) == 3


def test_drop_all_object_members_leaves_empty_object():
    resp = _redact({"a": {"x": 1, "y": 2}},
                   [{"pattern": "/a/x", "action": "drop"},
                    {"pattern": "/a/y", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"a": {}}


def test_mask_constant_can_be_any_json_value():
    resp = _redact({"a": 1, "b": 2},
                   [{"pattern": "/a", "action": "mask",
                     "value": {"redacted": [True, None]}}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"]["a"] == {"redacted": [True, None]}


def test_mask_constant_is_deep_copied_per_occurrence():
    resp = _redact({"a": 1, "b": 2},
                   [{"pattern": "/a", "action": "mask", "value": {"k": []}},
                    {"pattern": "/b", "action": "mask", "value": {"k": []}}])
    assert resp.status_code == 200, resp.text
    doc = resp.json()["document"]
    assert doc["a"] == {"k": []} and doc["b"] == {"k": []}
    assert doc["a"] is not doc["b"]
    assert doc["a"]["k"] is not doc["b"]["k"]


def test_input_document_is_not_mutated():
    doc = copy.deepcopy(DOC)
    resp = _redact(doc, [
        {"pattern": "/subject/name", "action": "mask", "value": "x"},
        {"pattern": "/emails/0", "action": "drop"},
        {"pattern": "/subject/age", "action": "drop"},
    ])
    assert resp.status_code == 200, resp.text
    assert doc == DOC


def test_escaped_rfc6901_literal_tokens_match():
    resp = _redact(DOC, [
        {"pattern": "/a~1b/tilde~0key", "action": "mask", "value": "X"},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"]["a/b"]["tilde~key"] == "X"


# ---------------------------------------------------------------------------
# Empty containers are leaves
# ---------------------------------------------------------------------------


def test_empty_object_and_empty_array_leaves_can_be_masked():
    resp = _redact(DOC, [
        {"pattern": "/emptyObj", "action": "mask", "value": None},
        {"pattern": "/emptyArr", "action": "mask", "value": "[]"},
    ])
    assert resp.status_code == 200, resp.text
    doc = resp.json()["document"]
    assert doc["emptyObj"] is None
    assert doc["emptyArr"] == "[]"
    paths = [entry["path"] for entry in resp.json()["applied"]]
    assert paths == ["/emptyArr", "/emptyObj"]


def test_empty_container_leaf_can_be_dropped():
    resp = _redact({"a": {}, "b": [], "c": [1]},
                   [{"pattern": "/a", "action": "drop"},
                    {"pattern": "/b", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"c": [1]}


def test_empty_array_element_drop_keeps_index():
    # An empty array nested inside an array is itself a leaf; dropping it
    # nulls that element rather than collapsing the outer array.
    resp = _redact([[], [1]], [{"pattern": "/0", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == [None, [1]]


# ---------------------------------------------------------------------------
# '*' wildcard (exactly one level)
# ---------------------------------------------------------------------------


def test_star_matches_exactly_one_level():
    resp = _redact(DOC, [{"pattern": "/subject/*", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    # Every direct member of /subject is dropped; the object shell remains.
    assert resp.json()["document"]["subject"] == {}
    # Nothing outside /subject is touched.
    assert resp.json()["document"]["emails"] == DOC["emails"]
    paths = [entry["path"] for entry in resp.json()["applied"]]
    assert paths == [
        "/subject/age",
        "/subject/id",
        "/subject/isAdult",
        "/subject/name",
        "/subject/nickname",
    ]


def test_star_does_not_cross_multiple_levels():
    # /nested/* must not reach /nested/deep/secret (two levels below).
    resp = _redact(DOC, [{"pattern": "/nested/*", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    # /nested/deep is a non-empty container: never matched, stays in place.
    assert resp.json()["document"]["nested"] == {"deep": {"secret": 7, "other": 8}}
    assert resp.json()["applied"] == []


def test_star_matches_array_indices():
    resp = _redact({"xs": ["a", "b", "c"]},
                   [{"pattern": "/xs/*", "action": "mask", "value": "m"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"xs": ["m", "m", "m"]}
    assert [e["rule"] for e in resp.json()["applied"]] == [0, 0, 0]


def test_star_matches_member_names_that_look_like_indices():
    # Object member "0" is addressed by the literal token, not array index.
    resp = _redact({"m": {"0": "x", "1": "y"}},
                   [{"pattern": "/m/*", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"m": {}}


def test_multiple_stars_each_consume_one_level():
    resp = _redact(DOC, [{"pattern": "/nested/*/*", "action": "mask",
                          "value": 0}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"]["nested"] == {"deep": {"secret": 0, "other": 0}}


# ---------------------------------------------------------------------------
# '**' wildcard (any number of levels, including zero)
# ---------------------------------------------------------------------------


def test_double_star_matches_descendants_at_any_depth():
    resp = _redact(DOC, [{"pattern": "/nested/**", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    # Both descendant leaves are removed; the intermediate container shell
    # '/nested/deep' was not itself a leaf and survives as an empty object.
    assert resp.json()["document"]["nested"] == {"deep": {}}
    paths = [entry["path"] for entry in resp.json()["applied"]]
    assert paths == ["/nested/deep/other", "/nested/deep/secret"]


def test_double_star_matches_zero_levels_on_a_leaf():
    # /subject/name is itself a leaf; '/subject/name/**' matches it with
    # ** consuming zero tokens.
    resp = _redact(DOC, [{"pattern": "/subject/name/**", "action": "mask",
                          "value": "X"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"]["subject"]["name"] == "X"
    assert [e["path"] for e in resp.json()["applied"]] == ["/subject/name"]


def test_double_star_matches_zero_levels_but_not_intermediate_containers():
    # '/nested/**' does NOT match the non-empty container /nested/deep.
    resp = _redact({"nested": {"deep": {"secret": 7}}},
                   [{"pattern": "/nested/**", "action": "mask", "value": 0}])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"] == {"nested": {"deep": {"secret": 0}}}
    assert [e["path"] for e in body["applied"]] == ["/nested/deep/secret"]


def test_double_star_in_the_middle_matches_any_prefix_depth():
    doc = {"a": {"b": {"leaf": 1}}, "leaf": 2}
    resp = _redact(doc, [{"pattern": "/**/leaf", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"a": {"b": {}}}
    paths = [e["path"] for e in resp.json()["applied"]]
    assert paths == ["/a/b/leaf", "/leaf"]


def test_double_star_only_matches_every_leaf_including_root():
    # '/**' consumes zero or more tokens after the root, so the root leaf
    # itself matches too (a bare '**' is not an RFC 6901 fragment and is a
    # POINTER_SYNTAX error).
    resp = _redact({"a": 1, "b": {"c": []}},
                   [{"pattern": "/**", "action": "mask", "value": "R"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"a": "R", "b": {"c": "R"}}

    # The root leaf of a scalar/empty document matches '/**' (zero tokens).
    resp = _redact(42, [{"pattern": "/**", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] is None
    assert resp.json()["applied"] == [{"path": "", "action": "drop", "rule": 0}]


def test_pattern_without_leading_slash_is_pointer_syntax():
    resp = _redact({"a": 1}, [{"pattern": "**", "action": "drop"}])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "POINTER_SYNTAX"


def test_several_double_stars_compose():
    doc = {"a": {"b": {"c": {"leaf": 1}}}}
    resp = _redact(doc, [{"pattern": "/**/b/**/leaf", "action": "mask",
                          "value": "z"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {"a": {"b": {"c": {"leaf": "z"}}}}


# ---------------------------------------------------------------------------
# Root leaf (empty pointer)
# ---------------------------------------------------------------------------


def test_empty_pattern_matches_only_root_leaf():
    resp = _redact(42, [{"pattern": "", "action": "mask", "value": 0}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == 0
    assert resp.json()["applied"] == [{"path": "", "action": "mask", "rule": 0}]


def test_empty_pattern_does_not_match_inside_containers():
    resp = _redact({"a": 1}, [{"pattern": "", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    # Root is a non-empty container (not a leaf), so nothing is redacted.
    assert resp.json()["document"] == {"a": 1}
    assert resp.json()["applied"] == []


def test_drop_root_empty_container_leaf_becomes_null():
    resp = _redact({}, [{"pattern": "", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] is None


def test_mask_empty_root_array():
    resp = _redact([], [{"pattern": "", "action": "mask", "value": None}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] is None


# ---------------------------------------------------------------------------
# Conflict resolution: literals > '*' > '**', then rule order
# ---------------------------------------------------------------------------


def test_literal_rule_beats_star_and_double_star():
    rules = [
        {"pattern": "/subject/**", "action": "mask", "value": "any"},
        {"pattern": "/subject/*", "action": "mask", "value": "one"},
        {"pattern": "/subject/name", "action": "mask", "value": "lit"},
    ]
    resp = _redact(DOC, rules)
    assert resp.status_code == 200, resp.text
    subject = resp.json()["document"]["subject"]
    assert subject["name"] == "lit"          # literal wins
    assert subject["age"] == "one"           # single star beats **
    assert subject["id"] == "one"
    applied = {e["path"]: e["rule"] for e in resp.json()["applied"]}
    assert applied["/subject/name"] == 2
    assert applied["/subject/age"] == 1


def test_more_literals_wins_even_against_more_stars():
    # /a/*/* (0 literals, 2 stars) vs /a/b/* (1 literal, 1 star):
    # the literal count decides regardless of star count.
    doc = {"a": {"b": {"c": 1}}}
    rules = [
        {"pattern": "/a/*/*", "action": "mask", "value": "stars"},
        {"pattern": "/a/b/*", "action": "mask", "value": "lit"},
    ]
    resp = _redact(doc, rules)
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"]["a"]["b"]["c"] == "lit"
    assert resp.json()["applied"][0]["rule"] == 1


def test_single_star_beats_double_star_with_same_literal_count():
    doc = {"a": {"b": 1, "c": 2}}
    rules = [
        {"pattern": "/a/**", "action": "mask", "value": "dstar"},
        {"pattern": "/a/*", "action": "mask", "value": "star"},
    ]
    resp = _redact(doc, rules)
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"]["a"] == {"b": "star", "c": "star"}


def test_rule_order_is_the_final_tie_break():
    # /a/* and /*/b both match /a/b with one literal and one star.
    doc = {"a": {"b": 1}}
    resp = _redact(doc, [
        {"pattern": "/a/*", "action": "mask", "value": "first"},
        {"pattern": "/*/b", "action": "mask", "value": "second"},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"]["a"]["b"] == "first"
    assert resp.json()["applied"][0]["rule"] == 0

    # Swap order -> the other rule wins.
    resp = _redact(doc, [
        {"pattern": "/*/b", "action": "mask", "value": "second"},
        {"pattern": "/a/*", "action": "mask", "value": "first"},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"]["a"]["b"] == "second"
    assert resp.json()["applied"][0]["rule"] == 0


def test_drop_loses_to_more_specific_mask():
    # A broad drop must not override a specific mask on the same leaf.
    rules = [
        {"pattern": "/subject/**", "action": "drop"},
        {"pattern": "/subject/name", "action": "mask", "value": "keep"},
    ]
    resp = _redact(DOC, rules)
    assert resp.status_code == 200, resp.text
    subject = resp.json()["document"]["subject"]
    assert subject == {"name": "keep"}  # other members dropped, name masked
    applied = {e["path"]: e for e in resp.json()["applied"]}
    assert applied["/subject/name"]["action"] == "mask"
    assert applied["/subject/age"]["action"] == "drop"


# ---------------------------------------------------------------------------
# No-ops, sorting and determinism
# ---------------------------------------------------------------------------


def test_empty_rules_list_returns_document_unchanged():
    resp = _redact(copy.deepcopy(DOC), [])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"document": DOC, "applied": []}


def test_patterns_matching_nothing_do_nothing():
    resp = _redact({"a": 1}, [{"pattern": "/missing/*", "action": "drop"}])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"document": {"a": 1}, "applied": []}


def test_applied_is_sorted_by_path_across_mixed_actions():
    rules = [
        {"pattern": "/z/**", "action": "mask", "value": 0},
        {"pattern": "/a", "action": "drop"},
        {"pattern": "/m/2", "action": "drop"},
        {"pattern": "/m/0", "action": "drop"},
        {"pattern": "/m/1", "action": "mask", "value": "x"},
    ]
    doc = {"z": {"deep": {"v": 1}}, "a": "a", "m": [0, 1, 2]}
    resp = _redact(doc, rules)
    assert resp.status_code == 200, resp.text
    paths = [entry["path"] for entry in resp.json()["applied"]]
    assert paths == ["/a", "/m/0", "/m/1", "/m/2", "/z/deep/v"]


def test_redaction_is_deterministic_across_calls():
    rules = [
        {"pattern": "/*/**", "action": "mask", "value": "x"},
        {"pattern": "/subject/name", "action": "mask", "value": "y"},
        {"pattern": "/emails/*", "action": "drop"},
    ]
    first = _redact(copy.deepcopy(DOC), rules)
    second = _redact(copy.deepcopy(DOC), rules)
    assert first.json() == second.json()


# ---------------------------------------------------------------------------
# Error handling: unified error format
# ---------------------------------------------------------------------------


def test_request_level_errors_match_other_endpoints():
    resp = client.post("/redact", json={"rules": []})
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "document"

    resp = client.post("/redact", json={"document": {}})
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "rules"

    resp = client.post("/redact", json=[1])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "body"

    resp = client.post(
        "/redact",
        content=b'{"document": {},',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_JSON"


def test_rules_must_be_an_array():
    resp = _redact({}, {"pattern": "/a", "action": "drop"})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    assert err["loc"] == "rules"


def test_rule_envelope_must_be_object():
    resp = _redact({}, ["drop", 42])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    assert err["loc"] == "rules[0]"


def test_missing_pattern_and_non_string_pattern():
    resp = _redact({}, [{"action": "drop"}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "rules[0].pattern"

    resp = _redact({}, [{"pattern": 7, "action": "drop"}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "rules[0].pattern"


@pytest.mark.parametrize(
    "pattern",
    [
        "no-leading-slash",
        "/bad~x",
        "/a~2b",
        "/trailing~",
    ],
)
def test_malformed_pattern_uses_pointer_syntax_with_precise_loc(pattern):
    resp = _redact({"a": 1}, [{"pattern": pattern, "action": "drop"}])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "POINTER_SYNTAX"
    assert err["loc"] == "rules[0].pattern"
    assert err["detail"]["pattern"] == pattern


def test_missing_or_invalid_action():
    resp = _redact({}, [{"pattern": "/a"}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "rules[0].action"

    resp = _redact({}, [{"pattern": "/a", "action": 42}])
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "rules[0].action"

    resp = _redact({}, [{"pattern": "/a", "action": "encrypt"}])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    assert err["loc"] == "rules[0].action"


def test_mask_requires_value():
    resp = _redact({"a": 1}, [{"pattern": "/a", "action": "mask"}])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    assert err["loc"] == "rules[0].value"


def test_drop_does_not_require_value_and_extra_fields_are_ignored():
    resp = _redact({"a": 1},
                   [{"pattern": "/a", "action": "drop", "value": "ignored"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == {}


def test_non_finite_mask_value_is_rejected_at_value_loc():
    resp = client.post(
        "/redact",
        content=b'{"document": {"a": 1}, "rules": ['
                b'{"pattern": "/a", "action": "mask", "value": NaN}]}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "UNSUPPORTED_VALUE"
    assert err["loc"] == "rules[0].value"


def test_error_loc_points_at_the_failing_rule_index():
    rules = [
        {"pattern": "/a", "action": "mask", "value": 1},
        {"pattern": "/b~bad", "action": "drop"},
    ]
    resp = _redact({"a": 1}, rules)
    assert resp.status_code == 400
    assert resp.json()["error"]["loc"] == "rules[1].pattern"


def test_no_partial_result_is_returned_on_rule_error():
    doc = {"a": 1}
    resp = _redact(doc, [
        {"pattern": "/a", "action": "mask", "value": 2},
        {"pattern": "nope", "action": "drop"},
    ])
    assert resp.status_code == 400
    body = resp.json()
    assert "document" not in body
    assert "applied" not in body
    assert doc == {"a": 1}


# ---------------------------------------------------------------------------
# Meta / OpenAPI
# ---------------------------------------------------------------------------


def test_redact_is_listed_in_service_metadata():
    info = client.get("/").json()
    assert info["endpoints"]["redact"] == "POST /redact"


def test_openapi_documents_redact_contract():
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/redact"]["post"]
    assert op["operationId"] == "redact"
    for name in ("RedactRule", "RedactRequest", "RedactResponse",
                 "AppliedRedaction"):
        assert name in schema["components"]["schemas"]
    assert "200" in op["responses"]
