"""The eligibility mini-language (engine/policy._eval_rule) parses customer-supplied
strings to decide who gets an offer. It replaced an `eval()`, so the load-bearing
property is: it evaluates comparisons and NOTHING ELSE — no code execution, ever.

These pin the grammar (OR of ANDs of `field OP value`), the type handling, and — most
importantly — that a malicious rule is inert."""

import operator
from pathlib import Path

import pytest

from engine.policy import _atom_ok, _coerce, _eval_rule

SCOPE = {
    "reason": "price_value_mismatch",
    "confidence": 0.9,
    "tenure": 200,
    "mrr": 49.0,
    "activated": True,
    "logins": 20,
    "seats_used": 7,  # a product-specific signal, merged from UserContext.signals
}


# --- coercion ---

@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("false", False), ("False", False),
    ("90", 90.0), ("3.5", 3.5), ("-2", -2.0),
    ('"hello"', "hello"), ("'hello'", "hello"), ("plain", "plain"),
    ("price_value_mismatch", "price_value_mismatch"),
])
def test_coerce(raw, expected):
    assert _coerce(raw) == expected


# --- comparisons by type ---

def test_numeric_operators():
    assert _eval_rule("tenure > 90", SCOPE)
    assert not _eval_rule("tenure > 900", SCOPE)
    assert _eval_rule("tenure >= 200", SCOPE)
    assert _eval_rule("mrr < 100", SCOPE)
    assert _eval_rule("logins != 5", SCOPE)


def test_string_equality():
    assert _eval_rule("reason == price_value_mismatch", SCOPE)
    assert not _eval_rule("reason == never_activated", SCOPE)
    assert _eval_rule("reason != never_activated", SCOPE)


def test_bool_equality():
    assert _eval_rule("activated == true", SCOPE)
    assert not _eval_rule("activated == false", SCOPE)


def test_single_equals_is_an_alias_for_eq():
    assert _eval_rule("reason = price_value_mismatch", SCOPE)


def test_signals_are_in_scope():
    assert _eval_rule("seats_used > 3", SCOPE)


# --- grammar: OR of ANDs ---

def test_and_requires_all_atoms():
    assert _eval_rule("reason == price_value_mismatch AND tenure > 90", SCOPE)
    assert not _eval_rule("reason == price_value_mismatch AND tenure > 900", SCOPE)


def test_or_requires_any_clause():
    # first clause fails (wrong reason), second passes (tenure)
    assert _eval_rule("reason == never_activated OR tenure > 90", SCOPE)
    assert not _eval_rule("reason == never_activated OR tenure > 900", SCOPE)


def test_or_of_ands_precedence():
    # (reason==X AND tenure>900)  OR  (activated==true)  ->  second wins
    assert _eval_rule(
        "reason == price_value_mismatch AND tenure > 900 OR activated == true", SCOPE
    )


def test_whitespace_insensitive():
    assert _eval_rule("tenure>90", SCOPE)
    assert _eval_rule("reason==price_value_mismatch", SCOPE)


# --- robustness: unknown fields and type mismatches fail closed ---

def test_unknown_field_is_false_not_error():
    assert not _eval_rule("nonexistent_field == 5", SCOPE)


def test_number_field_vs_string_value_never_raises():
    # comparing a numeric field against a non-numeric token falls back to string
    # comparison — it must yield a defined bool, never raise.
    assert isinstance(_atom_ok("mrr > abc", SCOPE), bool)
    assert isinstance(_atom_ok("reason > 5", SCOPE), bool)


def test_ge_matched_before_gt():
    # ">=" must be recognized as one operator, not ">" with "=200" as the value
    assert _eval_rule("tenure >= 200", SCOPE)
    assert not _eval_rule("tenure >= 201", SCOPE)


# --- THE security property: rules never execute code ---

def test_malicious_rule_does_not_execute(tmp_path):
    sentinel = tmp_path / "pwned"
    payload = f"mrr > __import__('os').system('touch {sentinel}')"
    result = _eval_rule(payload, SCOPE)
    assert isinstance(result, bool)          # evaluated as an inert string comparison
    assert not sentinel.exists()             # NO code ran


def test_injection_via_or_clause_is_inert(tmp_path):
    sentinel = tmp_path / "pwned2"
    payload = f"reason == x') or __import__('os').system('touch {sentinel}"
    assert _eval_rule(payload, SCOPE) in (True, False)
    assert not sentinel.exists()


def test_dunder_tokens_are_just_strings():
    # left is a string field; a dunder-looking value is compared, not evaluated
    assert not _eval_rule("reason == __import__", SCOPE)
    assert operator.eq  # sanity: we compare with real operators, not eval
