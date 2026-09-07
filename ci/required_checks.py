"""UGA-12 - declarative required-check rendering and the offline evidence verifier.

This module is the executable spine for the remote-enforcement work (UGA-13 / UGA-14). It has
two pure functions and one strictly read-only live adapter:

* :func:`render_desired_ruleset` renders the desired managed branch ruleset for one repository
  from the UGA-10 identity contract (:func:`ci.promotion.required_check_contract`) plus an
  explicit :class:`RulesetTarget`. Identities come *only* from the contract; the repository,
  default branch, managed ruleset name, enforcement mode and check-provider binding are explicit
  project inputs on the target. It never reads workflow YAML, never calls GitHub, and never
  replaces unrelated rules or bypasses.
* :func:`verify_acceptance_evidence` is a pure judge over a captured acceptance-evidence record
  (:data:`ACCEPTANCE_EVIDENCE_SCHEMA`). It passes only when every contract identity has exactly
  one successful run, bound to a single core SHA, in the right repository, under the bound check
  provider, with complete run/step evidence, executed gate commands, matching named dispatch
  refs, and post-apply rules that equal :func:`render_desired_ruleset`. Producer ``head_sha``
  must equal the core SHA; consumer and promotion ``head_sha`` must equal the umbrella SHA whose
  captured gitlink equals the core SHA. The umbrella SHA is *not* required to equal the core
  SHA. Caller-supplied evidence can never redefine the required identity set.
* the ``fetch_*`` adapter mirrors :mod:`ci.promotion`: it collects check-run bodies, workflow
  job/step execution evidence and ruleset readbacks, following pagination, wrapping transport
  errors, and reading only the *name* of a token environment variable (never its value). It has
  no mutation or dispatch path.
* ``python -m ci.required_checks verify`` is a strictly offline CLI: it reads one fenced JSON
  acceptance record from a Markdown report, loads the current identity contract and the explicit
  targets, optionally cross-checks a rendered ``ci/desired_rulesets.json`` snapshot, and returns
  0 only for PASS.

This module is CI tooling; the installable runtime never imports it. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ci import contract
from ci import promotion

#: GitHub REST API root; overridable so tests never touch the network.
GITHUB_API_ROOT = promotion.GITHUB_API_ROOT

#: Re-exported so consumers derive - never copy - the required-check identity contract.
RequiredCheckContract = promotion.RequiredCheckContract
required_check_contract = promotion.required_check_contract

#: The acceptance-evidence record shape :func:`verify_acceptance_evidence` judges. This is a
#: description for operators and UGA-13 scaffolding, not a parser: the verifier reads the keys
#: named here and ignores everything else (so a caller cannot smuggle in a smaller identity set).
ACCEPTANCE_EVIDENCE_SCHEMA: Mapping[str, object] = {
    "version": 1,
    "top_level_keys": (
        "schema_version",
        "repositories",
        "reviewed_shas",
        "dispatch",
        "pagination",
        "runs",
        "applied_rulesets",
        "policy_preimages",
        "targets",
    ),
    "reviewed_shas_keys": ("core", "umbrella"),
    "dispatch_keys": ("producer_ref", "consumer_ref", "promotion_ref"),
    "run_keys": (
        "check_name",
        "role",
        "repository",
        "app_id",
        "head_sha",
        "umbrella_gitlink_sha",
        "status",
        "conclusion",
        "observed_at",
        "html_url",
        "dispatch_ref",
        "workflow_run",
        "steps",
    ),
    "workflow_run_keys": ("id", "run_attempt", "html_url"),
    "step_keys": ("name", "number", "started_at", "conclusion", "command"),
    "roles": ("producer", "consumer", "promotion"),
}


class EvidenceError(RuntimeError):
    """A single-cause, deterministic failure raised *before* a verdict is formed.

    Used for malformed offline input, a missing/blank credential, or a transport error - never
    to signal that the acceptance evidence simply did not pass (that is an
    :class:`AcceptanceResult` with ``ok=False``).
    """


# ---------------------------------------------------------------------------------------------
# Pure renderer
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RulesetTarget:
    """Explicit, project-owned settings for one managed branch ruleset.

    None of these values are derived from the identity-only contract or embedded as core
    defaults: the caller supplies them for the repository it actually owns.
    """

    repository: str
    role: str  # "producer" or "consumer"
    default_branch: str
    ruleset_name: str
    enforcement: str  # "active" | "evaluate" | "disabled"
    check_provider_id: int


def _identities_for_role(rcc: RequiredCheckContract, role: str) -> tuple[str, ...]:
    if role == "producer":
        return tuple(rcc.producer)
    if role == "consumer":
        return (*rcc.consumer, rcc.promotion)
    raise EvidenceError(f"unknown ruleset target role: {role!r}")


def render_desired_ruleset(
    rcc: RequiredCheckContract, target: RulesetTarget
) -> dict[str, object]:
    """Render the desired managed branch ruleset payload for ``target``.

    Pure: a function of ``rcc`` (identities only) and ``target`` (explicit project inputs). The
    producer ruleset requires the producer identities; the consumer ruleset requires the
    consumer matrix cells plus the promotion identity. The ``_repository`` / ``_role`` keys are
    local annotations for the caller, not GitHub API payload fields.
    """

    identities = _identities_for_role(rcc, target.role)
    return {
        "name": target.ruleset_name,
        "target": "branch",
        "enforcement": target.enforcement,
        "conditions": {
            "ref_name": {
                "include": [f"refs/heads/{target.default_branch}"],
                "exclude": [],
            }
        },
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "do_not_enforce_on_create": False,
                    "required_status_checks": [
                        {"context": identity, "integration_id": target.check_provider_id}
                        for identity in identities
                    ],
                },
            }
        ],
        "_repository": target.repository,
        "_role": target.role,
    }


# ---------------------------------------------------------------------------------------------
# Pure acceptance-evidence verifier
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AcceptanceResult:
    """The acceptance verdict plus the reasons that produced it (one reason per rule)."""

    ok: bool
    reasons: tuple[str, ...]
    core_sha: str
    umbrella_sha: str
    checked_identities: tuple[str, ...]

    def evidence(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "core_sha": self.core_sha,
            "umbrella_sha": self.umbrella_sha,
            "checked_identities": list(self.checked_identities),
            "reasons": list(self.reasons),
        }


def _expected_commands(identity: str, loaded: contract.Contract) -> list[str]:
    """The gate command lines that a successful run for ``identity`` must have executed."""

    if identity == promotion._LINT_TYPES_CHECK:
        gates = [loaded.gate("lint"), loaded.gate("types")]
    elif identity == promotion._COVERAGE_CHECK:
        gates = [loaded.gate("coverage")]
    elif " · " in identity:
        gate_id = identity.split(" · ", 1)[0]
        try:
            gates = [loaded.gate(gate_id)]
        except contract.ContractError:
            gates = [loaded.gate("installed-package")]
    else:
        gates = []
    return [" ".join(command.argv) for gate in gates for command in gate.commands]


def _check_run(
    run: Mapping[str, object],
    identity: str,
    role: str,
    target: RulesetTarget,
    expected_head: str,
    core_sha: str,
    dispatch: Mapping[str, object],
    loaded: contract.Contract,
    reasons: list[str],
) -> None:
    if run.get("repository") != target.repository:
        reasons.append(
            f"required check {identity!r} ran in repository {run.get('repository')!r}, "
            f"not {target.repository!r}"
        )
    if run.get("app_id") != target.check_provider_id:
        reasons.append(
            f"required check {identity!r} ran under check provider {run.get('app_id')!r}, "
            f"not the bound provider {target.check_provider_id!r}"
        )
    if expected_head and run.get("head_sha") != expected_head:
        label = "core" if role == "producer" else "umbrella"
        reasons.append(
            f"required check {identity!r} run head SHA {run.get('head_sha')!r} does not "
            f"match the reviewed {label} SHA {expected_head!r}"
        )
    if role in ("consumer", "promotion") and run.get("umbrella_gitlink_sha") != core_sha:
        reasons.append(
            f"required check {identity!r} captured gitlink SHA "
            f"{run.get('umbrella_gitlink_sha')!r} does not equal the core SHA {core_sha!r}"
        )
    if run.get("status") != "completed":
        reasons.append(
            f"required check {identity!r} is not completed (status {run.get('status')!r})"
        )
    if run.get("conclusion") != "success":
        reasons.append(
            f"required check {identity!r} concluded {run.get('conclusion')!r}, not 'success'"
        )
    if not run.get("observed_at"):
        reasons.append(f"required check {identity!r} has no observation time")
    if not run.get("html_url"):
        reasons.append(f"required check {identity!r} has no run URL")

    workflow_run = run.get("workflow_run")
    if not isinstance(workflow_run, Mapping) or not all(
        key in workflow_run for key in ACCEPTANCE_EVIDENCE_SCHEMA["workflow_run_keys"]
    ):
        reasons.append(f"required check {identity!r} has incomplete workflow-run evidence")
    else:
        attempt = workflow_run.get("run_attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool):
            reasons.append(f"required check {identity!r} has an ambiguous run attempt")

    ref_key = f"{role}_ref"
    if dispatch and run.get("dispatch_ref") != dispatch.get(ref_key):
        reasons.append(
            f"required check {identity!r} dispatch ref {run.get('dispatch_ref')!r} does not "
            f"match the named {ref_key} {dispatch.get(ref_key)!r}"
        )

    steps = run.get("steps")
    if not isinstance(steps, list) or not steps:
        reasons.append(f"required check {identity!r} has no executed-step evidence")
        return

    succeeded_commands: set[str] = set()
    for step in steps:
        if not isinstance(step, Mapping):
            reasons.append(f"required check {identity!r} has a malformed step entry")
            continue
        if not step.get("started_at"):
            reasons.append(
                f"required check {identity!r} step {step.get('name')!r} never started "
                f"(shell-start failure)"
            )
        if step.get("conclusion") != "success":
            reasons.append(
                f"required check {identity!r} step {step.get('name')!r} did not succeed "
                f"(conclusion {step.get('conclusion')!r})"
            )
            continue
        if step.get("command"):
            succeeded_commands.add(str(step["command"]))

    for expected in _expected_commands(identity, loaded):
        if expected not in succeeded_commands:
            reasons.append(
                f"required check {identity!r} has no successful step running gate command "
                f"{expected!r} (a green workflow conclusion alone is insufficient)"
            )


def verify_acceptance_evidence(
    evidence: object,
    loaded: contract.Contract,
    targets: Sequence[RulesetTarget],
) -> AcceptanceResult:
    """Judge a captured acceptance-evidence record. Pure and fail-closed."""

    reasons: list[str] = []
    rcc = promotion.required_check_contract(loaded)

    if not isinstance(evidence, Mapping):
        return AcceptanceResult(
            False, ("acceptance evidence is not an object",), "", "", ()
        )

    if evidence.get("schema_version") != ACCEPTANCE_EVIDENCE_SCHEMA["version"]:
        reasons.append(
            f"acceptance evidence schema_version {evidence.get('schema_version')!r} is not "
            f"{ACCEPTANCE_EVIDENCE_SCHEMA['version']}"
        )

    producer_t = next((t for t in targets if t.role == "producer"), None)
    consumer_t = next((t for t in targets if t.role == "consumer"), None)
    if producer_t is None or consumer_t is None:
        reasons.append(
            "targets must supply exactly one 'producer' and one 'consumer' role"
        )
        return AcceptanceResult(False, tuple(reasons), "", "", ())

    shas = evidence.get("reviewed_shas")
    core_sha = ""
    umbrella_sha = ""
    if not isinstance(shas, Mapping):
        reasons.append("acceptance evidence has no reviewed_shas mapping")
    else:
        core_sha = str(shas.get("core") or "")
        umbrella_sha = str(shas.get("umbrella") or "")
        if not core_sha:
            reasons.append("reviewed_shas.core is absent")
        if not umbrella_sha:
            reasons.append("reviewed_shas.umbrella is absent")

    repos = evidence.get("repositories")
    if not isinstance(repos, Mapping):
        reasons.append("acceptance evidence has no repositories mapping")
    else:
        if repos.get("producer") != producer_t.repository:
            reasons.append(
                f"producer repository {repos.get('producer')!r} does not match the target "
                f"{producer_t.repository!r}"
            )
        if repos.get("consumer") != consumer_t.repository:
            reasons.append(
                f"consumer repository {repos.get('consumer')!r} does not match the target "
                f"{consumer_t.repository!r}"
            )

    pagination = evidence.get("pagination")
    if not isinstance(pagination, Mapping) or pagination.get("complete") is not True:
        reasons.append("check-run pagination is incomplete or unproven")

    dispatch = evidence.get("dispatch")
    if not isinstance(dispatch, Mapping):
        reasons.append("acceptance evidence has no dispatch mapping")
        dispatch = {}
    else:
        for key in ACCEPTANCE_EVIDENCE_SCHEMA["dispatch_keys"]:
            if not dispatch.get(key):
                reasons.append(f"acceptance evidence has no named dispatch {key}")

    preimages = evidence.get("policy_preimages")
    if not isinstance(preimages, Mapping):
        reasons.append("acceptance evidence retains no raw policy pre-images")
    else:
        for repo in (producer_t.repository, consumer_t.repository):
            if repo not in preimages:
                reasons.append(f"policy pre-image for {repo!r} is missing")

    runs = evidence.get("runs")
    if not isinstance(runs, list):
        reasons.append("acceptance evidence has no runs array")
        runs = []

    rows: list[tuple[str, str, RulesetTarget, str]] = [
        *[(identity, "producer", producer_t, core_sha) for identity in rcc.producer],
        *[(identity, "consumer", consumer_t, umbrella_sha) for identity in rcc.consumer],
        (rcc.promotion, "promotion", consumer_t, umbrella_sha),
    ]

    checked: list[str] = []
    for identity, role, target, expected_head in rows:
        checked.append(identity)
        matches = [
            run
            for run in runs
            if isinstance(run, Mapping)
            and run.get("check_name") == identity
            and run.get("role") == role
        ]
        if not matches:
            reasons.append(
                f"required check {identity!r} ({role}) has no run bound to "
                f"{expected_head or 'the reviewed SHA'}"
            )
            continue
        if len(matches) > 1:
            distinct = {
                json.dumps(run.get("workflow_run"), sort_keys=True) for run in matches
            }
            if len(distinct) > 1:
                reasons.append(
                    f"required check {identity!r} has ambiguous, conflicting run attempts"
                )
        _check_run(
            matches[0],
            identity,
            role,
            target,
            expected_head,
            core_sha,
            dispatch,
            loaded,
            reasons,
        )

    applied = evidence.get("applied_rulesets")
    if not isinstance(applied, Mapping):
        reasons.append("acceptance evidence has no applied_rulesets readback")
    else:
        for target in (producer_t, consumer_t):
            want = render_desired_ruleset(rcc, target)
            if applied.get(target.role) != want:
                reasons.append(
                    f"post-apply ruleset for {target.role!r} does not match the renderer"
                )

    return AcceptanceResult(
        ok=not reasons,
        reasons=tuple(reasons),
        core_sha=core_sha,
        umbrella_sha=umbrella_sha,
        checked_identities=tuple(checked),
    )


# ---------------------------------------------------------------------------------------------
# Read-only live adapter - reads the *name* of a token env var, never its value.
# ---------------------------------------------------------------------------------------------


def redact(text: str, token_env: str | None) -> str:
    """Replace the value of ``token_env`` (if set) with ``***`` anywhere in ``text``."""

    if not token_env:
        return text
    value = os.environ.get(token_env)
    if value:
        text = text.replace(value, "***")
    return text


def _require_token(token_env: str | None) -> str:
    if not token_env:
        raise EvidenceError("live mode requires a token environment variable name")
    token = os.environ.get(token_env)
    if not token:
        raise EvidenceError(
            f"environment variable {token_env!r} is unset or empty; cannot read the "
            f"GitHub API"
        )
    return token


def _urlopen_json(url: str, token: str) -> tuple[object, str | None]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "feature-pipeline-required-checks",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
            link = response.headers.get("Link")
    except urllib.error.URLError as exc:
        raise EvidenceError(f"GitHub API request to {url} failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"GitHub API response for {url} is not JSON: {exc}") from exc
    return body, promotion._next_link(link)


def _paginate(url: str, token: str, call, key: str) -> dict[str, object]:
    api_urls: list[str] = []
    items: list[object] = []
    total: int | None = None
    cursor: str | None = url
    while cursor is not None:
        api_urls.append(cursor)
        try:
            body, next_url = call(cursor, token)
        except EvidenceError:
            raise
        except urllib.error.URLError as exc:  # pragma: no cover - defensive
            raise EvidenceError(f"GitHub API request to {cursor} failed: {exc}") from exc
        if not isinstance(body, Mapping):
            raise EvidenceError(f"GitHub API response for {cursor} is not an object")
        page = body.get(key)
        if not isinstance(page, list):
            raise EvidenceError(
                f"GitHub API response for {cursor} has no {key!r} array"
            )
        if total is None:
            candidate = body.get("total_count")
            if not isinstance(candidate, int) or isinstance(candidate, bool):
                raise EvidenceError(
                    f"GitHub API response for {cursor} has no integer 'total_count'"
                )
            total = candidate
        items.extend(page)
        cursor = next_url
        if not page:
            break
    return {
        "total_count": total if total is not None else -1,
        key: items,
        "api_urls": api_urls,
    }


def fetch_check_runs(
    repository: str,
    sha: str,
    *,
    token_env: str | None,
    opener=None,
    api_root: str = GITHUB_API_ROOT,
) -> dict[str, object]:
    """Read every check run for ``sha`` in ``repository``, following pagination. Read-only."""

    token = _require_token(token_env)
    url = f"{api_root}/repos/{repository}/commits/{sha}/check-runs?per_page=100"
    return _paginate(url, token, opener or _urlopen_json, "check_runs")


def fetch_run_jobs(
    repository: str,
    run_id: int | str,
    *,
    token_env: str | None,
    opener=None,
    api_root: str = GITHUB_API_ROOT,
) -> dict[str, object]:
    """Read the jobs (and their steps) of one workflow run, following pagination. Read-only."""

    token = _require_token(token_env)
    url = f"{api_root}/repos/{repository}/actions/runs/{run_id}/jobs?per_page=100"
    return _paginate(url, token, opener or _urlopen_json, "jobs")


def fetch_ruleset(
    repository: str,
    ruleset_id: int | str,
    *,
    token_env: str | None,
    opener=None,
    api_root: str = GITHUB_API_ROOT,
) -> object:
    """Read one repository ruleset back for post-apply comparison. Read-only."""

    token = _require_token(token_env)
    call = opener or _urlopen_json
    body, _ = call(f"{api_root}/repos/{repository}/rulesets/{ruleset_id}", token)
    if not isinstance(body, Mapping):
        raise EvidenceError("ruleset readback response is not an object")
    return body


# ---------------------------------------------------------------------------------------------
# Offline CLI
# ---------------------------------------------------------------------------------------------


_FENCED_JSON = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


def _one_fenced_json(text: str) -> object:
    blocks = _FENCED_JSON.findall(text)
    if len(blocks) != 1:
        raise EvidenceError(
            f"acceptance report must contain exactly one fenced ```json block, "
            f"found {len(blocks)}"
        )
    try:
        return json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        raise EvidenceError(
            f"the fenced acceptance record is not valid JSON: {exc}"
        ) from exc


def _targets_from_record(record: Mapping[str, object]) -> tuple[RulesetTarget, ...]:
    raw = record.get("targets")
    if not isinstance(raw, list) or not raw:
        raise EvidenceError("acceptance record has no explicit 'targets' array")
    targets: list[RulesetTarget] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise EvidenceError("each entry of 'targets' must be an object")
        try:
            targets.append(
                RulesetTarget(
                    repository=str(item["repository"]),
                    role=str(item["role"]),
                    default_branch=str(item["default_branch"]),
                    ruleset_name=str(item["ruleset_name"]),
                    enforcement=str(item["enforcement"]),
                    check_provider_id=int(item["check_provider_id"]),
                )
            )
        except KeyError as exc:
            raise EvidenceError(f"a target is missing key {exc}") from exc
        except (TypeError, ValueError) as exc:
            raise EvidenceError(f"a target has an invalid field: {exc}") from exc
    return tuple(targets)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ci/required_checks.py",
        description="Offline required-check rendering and acceptance-evidence verification.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser(
        "verify",
        help="offline: judge one captured acceptance record against the identity contract",
    )
    verify.add_argument("--source-root", dest="source_root", required=True)
    verify.add_argument(
        "--desired",
        default=None,
        help="optional rendered ci/desired_rulesets.json snapshot to cross-check",
    )
    verify.add_argument("--evidence", required=True)
    return parser


def main(argv: Sequence[str] | None = None, *, stdout=None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    out = stdout if stdout is not None else sys.stdout

    try:
        root = Path(args.source_root)
        loaded = contract.load(root / "ci" / "gates.toml")
        record = _one_fenced_json(Path(args.evidence).read_text(encoding="utf-8"))
        if not isinstance(record, Mapping):
            raise EvidenceError("the fenced acceptance record is not an object")
        targets = _targets_from_record(record)
        rcc = promotion.required_check_contract(loaded)

        if args.desired:
            try:
                desired = json.loads(Path(args.desired).read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise EvidenceError(f"--desired snapshot is not valid JSON: {exc}") from exc
            for target in targets:
                if desired.get(target.role) != render_desired_ruleset(rcc, target):
                    print(
                        f"error: desired ruleset for {target.role!r} does not match the "
                        f"renderer",
                        file=sys.stderr,
                    )
                    return 1

        result = verify_acceptance_evidence(record, loaded, targets)
    except (EvidenceError, contract.ContractError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result.evidence(), indent=2, sort_keys=True), file=out)
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
