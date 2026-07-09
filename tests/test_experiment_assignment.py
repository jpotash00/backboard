"""Deterministic arm assignment: stable, uniform, and off by default."""

from api.experiment import CONTROL, TREATMENT, assign


def test_off_by_default_everyone_treated():
    # holdout_fraction 0.0 => experiment off => no save ever withheld.
    assert all(
        assign("default", "acme", f"u{i}", 0.0) == TREATMENT for i in range(200)
    )


def test_full_holdout_holds_everyone():
    assert all(
        assign("default", "acme", f"u{i}", 1.0) == CONTROL for i in range(200)
    )


def test_assignment_is_stable_across_calls():
    # Same key -> same arm, every time (a retried request can't flip the arm).
    first = assign("default", "acme", "user-42", 0.5)
    for _ in range(50):
        assert assign("default", "acme", "user-42", 0.5) == first


def test_fraction_is_approximately_honored():
    # Uniform hash => control share converges to holdout_fraction over many users.
    n = 5000
    controls = sum(
        1 for i in range(n) if assign("exp1", "acme", f"user-{i}", 0.2) == CONTROL
    )
    share = controls / n
    assert 0.17 < share < 0.23  # ~0.20 with sampling slack


def test_experiment_id_redraws_assignment():
    # Changing the experiment id reshuffles who is held out (a clean new experiment),
    # so the two assignments must not be identical across the population.
    users = [f"user-{i}" for i in range(500)]
    a = [assign("exp_A", "acme", u, 0.5) for u in users]
    b = [assign("exp_B", "acme", u, 0.5) for u in users]
    assert a != b


def test_customer_isolation():
    # The same user_id under two customers is assigned independently (keys differ).
    users = [f"user-{i}" for i in range(500)]
    a = [assign("default", "cust_A", u, 0.5) for u in users]
    b = [assign("default", "cust_B", u, 0.5) for u in users]
    assert a != b
