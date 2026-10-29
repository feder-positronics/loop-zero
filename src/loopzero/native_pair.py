"""Native model-level exception. No configuration or external-report import authority."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from functools import partial

from loopzero import runners
from loopzero.types import ReviewResult

MODELS = ("gpt-6.1-sol", "gpt-6-astra")
HEADER = "<!-- loopzero:codex-evidence v=2 "


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def authority(repo, number, head):
    return f"{repo}#{number}@{head}"


def finding_ids(result):
    return sorted({digest(canonical(dataclasses.asdict(f))) for f in result.findings})


def valid_member(proof, model):
    if (
        not isinstance(proof, dict)
        or proof.get("version") != 1
        or not runners.valid_codex_evidence(proof)
        or not proof["complete"]
        or proof["chunk_count"] != 1
    ):
        return False
    call = proof["calls"][0]
    return (
        call["requested"]["model"] == model
        and call["reported"]["model"] == model
        and call["reported"]["effort"] is not None
        and call["reported"] == call["requested"]
        and call["inspection"] == "verified"
        and call["fatal"] == "absent"
    )


def member_evidence(member, model):
    if (
        member.members
        or member.pair_authority
        or member.chunk_count != 1
        or member.kind != "primary"
    ):
        raise runners.RunnerBadOutput("native pair members must remain separate full 1/1 reviews")
    native = json.loads(member.raw)
    if any(
        e.get("type") == "error" or e.get("item", {}).get("type") == "error"
        for e in native.get("events", [])
    ):
        raise runners.RunnerBadOutput("native pair contains a native error event; HOLD")
    proof = runners.codex_evidence(member)
    if not valid_member(proof, model):
        raise runners.RunnerBadOutput(
            "native pair actual model/inspection evidence unknown or invalid"
        )
    return proof


def compose(members, grant, note="", *, prompt_sha256=None):
    if len(members) != 2 or any(
        r.members or r.pair_authority or r.pair_prompt_sha256 is not None for r in members
    ):
        raise runners.RunnerBadOutput("native pair requires two separate full reviews")
    first = members[0]
    findings = tuple(dict.fromkeys(f for r in members for f in r.findings))
    result = ReviewResult(
        family="codex",
        head=first.head,
        kind="primary",
        verdict="request_changes"
        if any(r.verdict == "request_changes" for r in members)
        else "approve",
        findings=findings,
        raw=canonical({"type": "codex_review_pair", "members": [r.raw for r in members]}),
        model=" + ".join(MODELS),
        provenance={
            "family": "codex",
            "session_ids": [r.provenance.get("session_id") for r in members],
        },
        members=tuple(members),
        reconciliation=note,
        pair_authority=grant,
        pair_prompt_sha256=prompt_sha256,
    )
    # Complete captures may be retained even while explicit reconciliation is pending.
    evidence(result, require_reconciliation=False)
    return result


def evidence(result, *, require_reconciliation=True):
    if len(result.members) != 2 or result.family != "codex" or result.kind != "primary":
        raise runners.RunnerBadOutput("invalid native pair shape")
    if not runners._hex(result.pair_prompt_sha256, 64):
        raise runners.RunnerBadOutput("native pair original task/prompt commitment missing")
    members = []
    for model, member in zip(MODELS, result.members, strict=True):
        proof = member_evidence(member, model)
        prompt = json.loads(member.raw)["review_context"]["prompt_sha256"]
        if prompt != result.pair_prompt_sha256:
            raise runners.RunnerBadOutput("native pair shared original task/prompt mismatch")
        members.append(
            {
                "evidence": proof,
                "verdict": member.verdict,
                "findings": finding_ids(member),
                "provenance_sha256": digest(canonical(member.provenance)),
                "native_errors": 0,
                "prompt_sha256": prompt,
            }
        )
    first = members[0]["evidence"]
    binding = ("repo", "pr", "head", "kind", "publisher")
    if any(
        tuple(m["evidence"][k] for k in binding) != tuple(first[k] for k in binding)
        for m in members
    ):
        raise runners.RunnerBadOutput("native pair publication context mismatch")
    if result.head != first["head"] or result.pair_authority != authority(
        first["repo"], first["pr"], result.head
    ):
        raise runners.RunnerBadOutput("native pair requires exact trusted operator context")
    calls = [m["evidence"]["calls"][0] for m in members]
    if calls[0]["session"] == calls[1]["session"] or (
        calls[0]["base"],
        calls[0]["diff_sha256"],
    ) != (calls[1]["base"], calls[1]["diff_sha256"]):
        raise runners.RunnerBadOutput("native pair session/range mismatch")
    union = tuple(dict.fromkeys(f for r in result.members for f in r.findings))
    verdict = (
        "request_changes"
        if any(r.verdict == "request_changes" for r in result.members)
        else "approve"
    )
    if (
        result.findings != union
        or result.verdict != verdict
        or result.chunk_count != 1
        or result.raw
        != canonical({"type": "codex_review_pair", "members": [r.raw for r in result.members]})
        or result.provenance != {"family": "codex", "session_ids": [c["session"] for c in calls]}
    ):
        raise runners.RunnerBadOutput("native pair union/raw/provenance changed")
    disagreements = (
        members[0]["findings"] != members[1]["findings"]
        or members[0]["verdict"] != members[1]["verdict"]
    )
    if not isinstance(result.reconciliation, str) or len(result.reconciliation.encode()) > 2048:
        raise runners.RunnerBadOutput("invalid native pair reconciliation")
    if require_reconciliation and disagreements and not result.reconciliation.strip():
        raise runners.RunnerBadOutput(
            "native pair disagreements require explicit reconciliation; HOLD"
        )
    value = {
        "version": 2,
        "family": "codex",
        **{k: first[k] for k in binding},
        "authority": result.pair_authority,
        "prompt_sha256": result.pair_prompt_sha256,
        "raw_sha256": digest(result.raw),
        "raw_bytes": len(result.raw.encode()),
        "complete": True,
        "members": members,
        "verdict": verdict,
        "union": sorted({x for m in members for x in m["findings"]}),
        "disagreements": disagreements,
        "differences": {
            MODELS[i]: sorted(set(members[i]["findings"]) - set(members[1 - i]["findings"]))
            for i in range(2)
        },
        "reconciliation": digest(result.reconciliation)
        if result.reconciliation.strip()
        else "unanimous-union",
    }
    value["binding_sha256"] = digest(canonical(value))
    return value


def valid_public(value):
    fields = {
        "version",
        "family",
        "repo",
        "pr",
        "head",
        "kind",
        "publisher",
        "authority",
        "prompt_sha256",
        "raw_sha256",
        "raw_bytes",
        "complete",
        "members",
        "verdict",
        "union",
        "disagreements",
        "differences",
        "reconciliation",
        "binding_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or type(value["version"]) is not int
        or value["version"] != 2
    ):
        return False
    if (
        value["family"] != "codex"
        or value["kind"] != "primary"
        or value["complete"] is not True
        or not runners._bounded_int(value["pr"])
        or not runners._hex(value["prompt_sha256"], 64)
        or not runners._hex(value["raw_sha256"], 64)
        or not runners._bounded_int(value["raw_bytes"])
        or not isinstance(value["members"], list)
        or len(value["members"]) != 2
        or value["authority"] != authority(value["repo"], value["pr"], value["head"])
    ):
        return False
    unsigned = {k: v for k, v in value.items() if k != "binding_sha256"}
    if value["binding_sha256"] != digest(canonical(unsigned)):
        return False
    sessions, ranges, findings, verdicts = [], [], [], []
    for model, member in zip(MODELS, value["members"], strict=True):
        if not isinstance(member, dict) or set(member) != {
            "evidence",
            "verdict",
            "findings",
            "provenance_sha256",
            "native_errors",
            "prompt_sha256",
        }:
            return False
        proof = member["evidence"]
        if not valid_member(proof, model) or any(
            proof[k] != value[k] for k in ("repo", "pr", "head", "kind", "publisher")
        ):
            return False
        call = proof["calls"][0]
        if (
            member["verdict"] not in ("approve", "request_changes")
            or type(member["native_errors"]) is not int
            or member["native_errors"] != 0
            or member["prompt_sha256"] != value["prompt_sha256"]
            or not runners._hex(member["provenance_sha256"], 64)
            or not isinstance(member["findings"], list)
            or any(not runners._hex(f, 64) for f in member["findings"])
            or sorted(set(member["findings"])) != member["findings"]
        ):
            return False
        sessions.append(call["session"])
        ranges.append((call["base"], call["diff_sha256"]))
        findings.append(member["findings"])
        verdicts.append(member["verdict"])
    different = findings[0] != findings[1] or verdicts[0] != verdicts[1]
    return (
        len(set(sessions)) == 2
        and len(set(ranges)) == 1
        and value["union"] == sorted(set(findings[0] + findings[1]))
        and value["verdict"] == ("request_changes" if "request_changes" in verdicts else "approve")
        and type(value["disagreements"]) is bool
        and value["disagreements"] == different
        and value["differences"]
        == {MODELS[i]: sorted(set(findings[i]) - set(findings[1 - i])) for i in range(2)}
        and (
            runners._hex(value["reconciliation"], 64)
            or not different
            and value["reconciliation"] == "unanimous-union"
        )
    )


def request(config, head, kind, diff, task, publication, grant, effort, base):
    if kind != "primary" or not publication or grant != authority(*publication[:2], head):
        raise runners.RunnerBadOutput(
            "native pair requires exact operator repo#PR@head primary context"
        )
    settings = config.review.get("codex")
    if settings is None or "codex" not in config.reviewers:
        raise runners.RunnerBadOutput("native pair requires configured Codex settings")
    chosen = effort or (settings.effort if settings else None)
    if (
        not chosen
        or settings
        and settings.allowed_efforts
        and chosen not in settings.allowed_efforts
    ):
        raise runners.RunnerBadOutput("native pair effort is not configured/allowed")
    if not runners._hex(base, 40) or not runners._hex(head, 40):
        raise runners.RunnerBadOutput("native pair requires immutable full head/base")
    prompt = runners.build_prompt(kind=kind, head=head, task_text=task, diff=diff)
    return chosen, digest(prompt)


def run(cwd, config, head, kind, diff, task, publication, grant, note, effort, base, capture=None):
    chosen, prompt = request(config, head, kind, diff, task, publication, grant, effort, base)
    repo, number, publisher = publication
    members = []
    for model in MODELS:
        captured = runners.review_with(
            "codex",
            cwd=cwd,
            head=head,
            kind=kind,
            diff=diff,
            task_text=task,
            reviewer_ro_paths=config.reviewer_ro_paths,
            model=model,
            effort=chosen,
            review_context={
                "repo": repo,
                "pr": number,
                "publisher": publisher,
                "head": head,
                "kind": kind,
                "base": base,
                "diff_sha256": digest(diff),
                "diff_bytes": len(diff.encode()),
            },
            **({"capture": partial(capture, len(members) + 1)} if capture else {}),
        )
        member_evidence(captured, model)
        native = json.loads(captured.raw)
        context = native.get("review_context", {})
        expected = {
            "repo": repo,
            "pr": number,
            "publisher": publisher,
            "head": head,
            "kind": kind,
            "base": base,
            "diff_sha256": digest(diff),
            "diff_bytes": len(diff.encode()),
            "cwd": str(cwd.resolve()),
            "index": 1,
            "chunks": 1,
            "fragment_sha256": digest(diff),
            "prompt_sha256": prompt,
            "requested": {"model": model, "effort": chosen},
        }
        if any(context.get(k) != value for k, value in expected.items()):
            raise runners.RunnerBadOutput("native pair capture differs from caller full input")
        members.append(captured)
    return compose(members, grant, note, prompt_sha256=prompt)
