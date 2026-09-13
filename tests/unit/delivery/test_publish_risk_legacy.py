import importlib
from pathlib import Path
from types import ModuleType

import pytest


def load_module() -> ModuleType:
    return importlib.import_module("loopzero.delivery._publish_risk")


module = load_module()


def test_accepted_legacy_review_is_marked_for_risk_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = {"task_contract": {"review_intent": "delivery-code-review"}}
    monkeypatch.setattr(
        module,
        "accepted_review_terminals",
        lambda _records: {"legacy-review": terminal},
    )

    assert module.load_review_risk_envelope([], "legacy-review") is None


def test_legacy_review_derives_risk_for_the_exact_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = object()
    derived = {
        "review_risk_json": '{"canonical":"risk"}',
        "review_risk_sha256": "a" * 64,
    }
    monkeypatch.setattr(
        module, "load_review_risk_envelope", lambda *_args, **_kwargs: None
    )

    def compute(repo, base, head, *, runner):
        assert (repo, base, head) == (tmp_path, "base-sha", "head-sha")
        assert runner is test_runner
        return derived

    test_runner = runner
    monkeypatch.setattr(module.delivery_review_risk, "compute_review_risk", compute)

    assert module.review_risk_envelope_for_publication(
        [], "legacy-review", tmp_path, "base-sha", "head-sha", test_runner
    ) == ('{"canonical":"risk"}', "a" * 64)


def test_exact_candidate_keeps_its_bound_risk_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    accepted = ('{"tier":"T1"}', "a" * 64)
    monkeypatch.setattr(
        module, "load_review_risk_envelope", lambda *_args, **_kwargs: accepted
    )
    monkeypatch.setattr(
        module.delivery_review_risk,
        "parse_review_risk",
        lambda _payload: {"base_sha": "base", "head_sha": "head"},
    )
    monkeypatch.setattr(
        module.delivery_review_risk,
        "compute_review_risk",
        lambda *_args, **_kwargs: pytest.fail("exact candidate must keep the envelope"),
    )

    assert (
        module.review_risk_envelope_for_publication(
            [], "review", tmp_path, "base", "head", object()
        )
        == accepted
    )


def test_changed_candidate_binding_rederives_risk_without_patch_carry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    accepted = ('{"candidate":"reviewed"}', "a" * 64)
    derived = {
        "review_risk_json": '{"candidate":"publication"}',
        "review_risk_sha256": "b" * 64,
    }
    monkeypatch.setattr(
        module, "load_review_risk_envelope", lambda *_args, **_kwargs: accepted
    )
    monkeypatch.setattr(
        module.delivery_review_risk,
        "parse_review_risk",
        lambda _payload: {"base_sha": "old-base", "head_sha": "old-head"},
    )
    monkeypatch.setattr(
        module.delivery_review_risk,
        "compute_review_risk",
        lambda *_args, **_kwargs: derived,
    )
    monkeypatch.setattr(
        module.delivery_review_risk,
        "verify_review_risk",
        lambda *_args, **_kwargs: {
            "effective_tier": "T1",
            "effective_security_trigger_paths": ["scripts/auth.py"],
        },
    )

    assert module.review_risk_envelope_for_publication(
        [], "review", tmp_path, "new-base", "new-head", object()
    ) == (derived["review_risk_json"], derived["review_risk_sha256"])


def test_patch_carried_review_rederives_risk_without_weakening_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    accepted = ('{"candidate":"reviewed"}', "a" * 64)
    derived = {
        "review_risk_json": '{"candidate":"publication"}',
        "review_risk_sha256": "b" * 64,
    }
    monkeypatch.setattr(
        module, "load_review_risk_envelope", lambda *_args, **_kwargs: accepted
    )
    monkeypatch.setattr(
        module.delivery_review_risk,
        "compute_review_risk",
        lambda *_args, **_kwargs: derived,
    )

    def verify(_repo, payload, _digest, *, runner):
        assert runner is test_runner
        if payload == accepted[0]:
            return {
                "effective_tier": "T1",
                "effective_security_trigger_paths": ["scripts/auth.py"],
            }
        assert payload == derived["review_risk_json"]
        return {
            "effective_tier": "T2",
            "effective_security_trigger_paths": [
                "scripts/auth.py",
                "scripts/worker.sh",
            ],
        }

    test_runner = object()
    monkeypatch.setattr(module.delivery_review_risk, "verify_review_risk", verify)

    assert module.review_risk_envelope_for_publication(
        [],
        "review",
        tmp_path,
        "new-base",
        "new-head",
        test_runner,
        patch_identity_covers_head=True,
    ) == (derived["review_risk_json"], derived["review_risk_sha256"])


@pytest.mark.parametrize(
    ("derived_tier", "derived_security", "message"),
    [
        ("T0", ["scripts/auth.py"], "tier became weaker"),
        ("T2", [], "security scope became weaker"),
    ],
)
def test_patch_carried_review_rejects_weaker_risk_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    derived_tier: str,
    derived_security: list[str],
    message: str,
) -> None:
    accepted = ('{"candidate":"reviewed"}', "a" * 64)
    derived = {
        "review_risk_json": '{"candidate":"publication"}',
        "review_risk_sha256": "b" * 64,
    }
    monkeypatch.setattr(
        module, "load_review_risk_envelope", lambda *_args, **_kwargs: accepted
    )
    monkeypatch.setattr(
        module.delivery_review_risk,
        "compute_review_risk",
        lambda *_args, **_kwargs: derived,
    )
    monkeypatch.setattr(
        module.delivery_review_risk,
        "verify_review_risk",
        lambda _repo, payload, _digest, *, runner: {
            "effective_tier": "T1" if payload == accepted[0] else derived_tier,
            "effective_security_trigger_paths": (
                ["scripts/auth.py"] if payload == accepted[0] else derived_security
            ),
        },
    )

    with pytest.raises(module.PublicationError, match=message):
        module.review_risk_envelope_for_publication(
            [],
            "review",
            tmp_path,
            "new-base",
            "new-head",
            object(),
            patch_identity_covers_head=True,
        )
