"""UGA-07 - the core-owned same-SHA promotion verifier.

A submodule gitlink update in the umbrella repository is a dependency promotion, not an
ordinary documentation bump. It may be accepted only when every required *producer* check is
successful for the **exact** gitlink SHA - never for a branch head, an unrelated commit, or
incomplete/ambiguous evidence.

This module owns that decision for the core repository:

* :func:`verify_promotion` is a pure function over a GitHub *check-runs* API body (a mapping
  with ``total_count`` and ``check_runs``). It fails closed - returning a
  :class:`PromotionResult` with ``ok=False`` and one reason per rule - when the SHA is absent,
  a required check is missing / pending / failing, the only evidence belongs to another SHA,
  pagination is incomplete (``total_count`` disagrees with the number of runs), or the body is
  malformed.
* :func:`required_core_checks` derives the required check identities from ``ci/gates.toml`` (the
  core ``quality-gates`` workflow's own check-name policy), so the identities are manifest data,
  never hard-coded inside the verifier. The umbrella ``installed-package`` consumer identity is
  deliberately *not* one of them (it is the separate downstream proof).
* :func:`fetch_check_runs` is the live GitHub API mode. It reads only the *name* of a token
  environment variable and never logs, returns, or embeds the token's value; an absent
  credential raises :class:`PromotionError` rather than silently passing.
* :meth:`PromotionResult.evidence` emits a redacted JSON-ready mapping naming the umbrella SHA,
  gitlink SHA, upstream repository, required checks, per-check conclusions, and API URLs, with
  no credential anywhere.

This module is CI tooling; the installable runtime never imports it. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ci import contract

#: GitHub REST API root; overridable so tests never touch the network.
GITHUB_API_ROOT = "https://api.github.com"

#: The umbrella ``installed-package`` consumer workflow identity. The required check identities
#: expand this across its OS/Python matrix below.
CONSUMER_CHECK_IDENTITY = "installed-package"

#: The stable required-check name rendered by the umbrella promotion workflow.
PROMOTION_CHECK_IDENTITY = "same-SHA core promotion"

#: Public compatibility version for :func:`required_check_contract` consumers.
REQUIRED_CHECK_CONTRACT_VERSION = 1

#: The core ``quality-gates`` workflow's stable check-name for the ``lint`` and ``types`` gates.
_LINT_TYPES_CHECK = "ruff + mypy (incl. complexity)"
#: ... and for the ``coverage`` gate.
_COVERAGE_CHECK = "coverage (branch, ratcheted floor)"

#: Keys every check-run object must carry for the verifier to reason about it.
_REQUIRED_RUN_KEYS = ("name", "head_sha", "status", "conclusion", "html_url")


class PromotionError(RuntimeError):
    """A single-cause, deterministic failure raised *before* a verdict is formed.

    Used for malformed offline input, a missing/blank credential, or a transport error - never
    to signal that the promotion simply did not pass (that is a :class:`PromotionResult`).
    """


@dataclass(frozen=True)
class CheckConclusion:
    """The selected check run for one required identity."""

    identity: str
    status: str
    conclusion: str | None
    head_sha: str
    url: str

    def as_dict(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "status": self.status,
            "conclusion": self.conclusion,
            "head_sha": self.head_sha,
            "url": self.url,
        }


@dataclass(frozen=True)
class PromotionResult:
    """The promotion verdict plus the redacted evidence that produced it."""

    ok: bool
    upstream_repo: str
    core_sha: str
    umbrella_sha: str | None
    required_checks: tuple[str, ...]
    conclusions: tuple[CheckConclusion, ...]
    reasons: tuple[str, ...]
    api_urls: tuple[str, ...]

    def evidence(self) -> dict[str, object]:
        """A JSON-ready, credential-free record of the decision (AC-4)."""

        return {
            "gate": "core-promotion",
            "ok": self.ok,
            "umbrella_sha": self.umbrella_sha,
            "gitlink_sha": self.core_sha,
            "upstream_repo": self.upstream_repo,
            "required_checks": list(self.required_checks),
            "conclusions": [
                {**c.as_dict(), "url": _redact_url(c.url)} for c in self.conclusions
            ],
            "api_urls": [_redact_url(url) for url in self.api_urls],
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class RequiredCheckContract:
    """All required-check identities, from one versioned executable contract."""

    version: int
    producer: tuple[str, ...]
    consumer: tuple[str, ...]
    promotion: str

    def all(self) -> tuple[str, ...]:
        """Return every required-check identity in stable display order."""

        return (*self.producer, *self.consumer, self.promotion)


def _redact_url(url: str) -> str:
    """Keep a URL's public location while dropping credentials and query values."""

    parsed = urllib.parse.urlsplit(url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    query = "&".join(f"{key}=***" for key, _ in urllib.parse.parse_qsl(parsed.query))
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))


def required_core_checks(loaded: contract.Contract) -> tuple[str, ...]:
    """The required producer check identities, derived from ``ci/gates.toml``.

    Mirrors the core ``quality-gates.yml`` check-name policy: ``lint``/``types`` share one
    ``ruff + mypy (incl. complexity)`` check, ``coverage`` has its own, and every other
    multi-OS ``core`` gate contributes one ``"<gate id> · <os>"`` check per runner. Gates with
    no OS matrix and no dedicated workflow check (e.g. ``documentation``) are not required
    remote checks.
    """

    names: list[str] = []
    for gate in loaded.group("core"):
        if gate.id in ("lint", "types"):
            if _LINT_TYPES_CHECK not in names:
                names.append(_LINT_TYPES_CHECK)
        elif gate.id == "coverage":
            if _COVERAGE_CHECK not in names:
                names.append(_COVERAGE_CHECK)
        elif gate.os:
            for image in gate.os:
                names.append(f"{gate.id} · {image}")
    return tuple(names)


def required_check_contract(loaded: contract.Contract) -> RequiredCheckContract:
    """Build the required-check contract without parsing workflow YAML or calling GitHub."""

    consumer_gate = loaded.gate(CONSUMER_CHECK_IDENTITY)
    consumer = tuple(
        f"{image} · py{version}"
        for image in consumer_gate.os
        for version in consumer_gate.python
    )
    return RequiredCheckContract(
        version=REQUIRED_CHECK_CONTRACT_VERSION,
        producer=required_core_checks(loaded),
        consumer=consumer,
        promotion=PROMOTION_CHECK_IDENTITY,
    )


def _extract_api_urls(payload: object) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        return ()
    raw = payload.get("api_urls")
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw)


def _parse_runs(payload: object, reasons: list[str]) -> list[dict] | None:
    """Return the validated check-run list, or ``None`` after appending a fail-closed reason."""

    if not isinstance(payload, Mapping):
        reasons.append("check-run payload is not an object")
        return None
    raw = payload.get("check_runs")
    total = payload.get("total_count")
    if not isinstance(raw, list):
        reasons.append("check-run payload has no 'check_runs' array")
        return None
    if not isinstance(total, int) or isinstance(total, bool):
        reasons.append("check-run payload has no integer 'total_count'")
        return None
    if total != len(raw):
        reasons.append(
            f"check-run pagination is incomplete: total_count {total} != "
            f"{len(raw)} run(s) received"
        )
        return None
    runs: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            reasons.append(f"check run #{index} is not an object")
            return None
        missing = [key for key in _REQUIRED_RUN_KEYS if key not in item]
        if missing:
            reasons.append(
                f"check run #{index} is missing key(s): {', '.join(missing)}"
            )
            return None
        if item["status"] == "completed" and not item.get("completed_at"):
            reasons.append(
                f"check run #{index} is 'completed' without a 'completed_at' timestamp"
            )
            return None
        runs.append(dict(item))
    return runs


def _select_per_identity(
    runs: Sequence[Mapping[str, object]], core_sha: str
) -> dict[str, tuple[str, dict]]:
    """Map each check name seen for ``core_sha`` to ``(kind, run)``.

    ``kind`` is ``"completed"`` for the deterministically latest completed run (by
    ``completed_at`` then ``id``), ``"pending"`` when no run for that name has finished, or
    ``"ambiguous"`` when two finished runs share a timestamp but disagree on the conclusion.
    """

    grouped: dict[str, list[dict]] = {}
    for run in runs:
        if run["head_sha"] != core_sha:
            continue
        grouped.setdefault(str(run["name"]), []).append(dict(run))

    selected: dict[str, tuple[str, dict]] = {}
    for name, group in grouped.items():
        completed = [run for run in group if run["status"] == "completed"]
        if not completed:
            newest = sorted(group, key=lambda run: str(run.get("id", "")))[-1]
            selected[name] = ("pending", newest)
            continue
        completed.sort(
            key=lambda run: (str(run["completed_at"]), str(run.get("id", "")))
        )
        top = completed[-1]
        rivals = [
            run
            for run in completed[:-1]
            if str(run["completed_at"]) == str(top["completed_at"])
            and run.get("conclusion") != top.get("conclusion")
        ]
        selected[name] = ("ambiguous" if rivals else "completed", top)
    return selected


def verify_promotion(
    *,
    upstream_repo: str,
    core_sha: str,
    required_checks: Sequence[str],
    payload: object,
    umbrella_sha: str | None = None,
) -> PromotionResult:
    """Decide whether a gitlink promotion to ``core_sha`` is allowed. Fails closed."""

    reasons: list[str] = []
    conclusions: list[CheckConclusion] = []
    api_urls = _extract_api_urls(payload)

    if not isinstance(core_sha, str) or not core_sha:
        reasons.append("core SHA is absent")
    if not upstream_repo:
        reasons.append("upstream repository is absent")
    if not tuple(required_checks):
        reasons.append("no required check identities were supplied")

    runs = _parse_runs(payload, reasons)

    if runs is not None and isinstance(core_sha, str) and core_sha:
        selected = _select_per_identity(runs, core_sha)
        for identity in required_checks:
            chosen = selected.get(identity)
            if chosen is None:
                if any(str(run["name"]) == identity for run in runs):
                    reasons.append(
                        f"required check {identity!r} has evidence only for another SHA"
                    )
                else:
                    reasons.append(
                        f"required check {identity!r} is missing for {core_sha}"
                    )
                continue
            kind, run = chosen
            conclusions.append(
                CheckConclusion(
                    identity=identity,
                    status=str(run["status"]),
                    conclusion=(
                        None if run.get("conclusion") is None else str(run["conclusion"])
                    ),
                    head_sha=str(run["head_sha"]),
                    url=str(run["html_url"]),
                )
            )
            if kind == "pending":
                reasons.append(
                    f"required check {identity!r} is not completed "
                    f"(status {str(run['status'])!r})"
                )
            elif kind == "ambiguous":
                reasons.append(
                    f"required check {identity!r} has ambiguous completed evidence "
                    f"for {core_sha}"
                )
            elif run.get("conclusion") != "success":
                reasons.append(
                    f"required check {identity!r} concluded "
                    f"{run.get('conclusion')!r}, not 'success'"
                )

    return PromotionResult(
        ok=not reasons,
        upstream_repo=upstream_repo,
        core_sha=core_sha if isinstance(core_sha, str) else "",
        umbrella_sha=umbrella_sha,
        required_checks=tuple(required_checks),
        conclusions=tuple(conclusions),
        reasons=tuple(reasons),
        api_urls=api_urls,
    )


# ---------------------------------------------------------------------------------------------
# Live GitHub API mode - reads the *name* of a token env var, never its value.
# ---------------------------------------------------------------------------------------------


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().lstrip("<").rstrip(">")
        if any(chunk.strip() == 'rel="next"' for chunk in section[1:]):
            return url
    return None


def _urlopen_json(url: str, token: str) -> tuple[object, str | None]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "feature-pipeline-core-promotion",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https root
            body = json.loads(response.read().decode("utf-8"))
            link = response.headers.get("Link")
    except urllib.error.URLError as exc:
        raise PromotionError(f"GitHub API request to {url} failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PromotionError(f"GitHub API response for {url} is not JSON: {exc}") from exc
    return body, _next_link(link)


def fetch_check_runs(
    upstream_repo: str,
    core_sha: str,
    *,
    token_env: str | None,
    opener=None,
    api_root: str = GITHUB_API_ROOT,
) -> dict[str, object]:
    """Fetch every check run for ``core_sha`` in ``upstream_repo``, following pagination.

    ``token_env`` names the environment variable that holds the credential; its value is read
    here, passed to ``opener``, and never logged or returned. An unset/blank variable raises
    :class:`PromotionError`. ``opener(url, token) -> (body, next_url | None)`` is injected by
    tests so no real request is made.
    """

    if not token_env:
        raise PromotionError("live mode requires --token-env naming a credential variable")
    token = os.environ.get(token_env)
    if not token:
        raise PromotionError(
            f"environment variable {token_env!r} is unset or empty; cannot read "
            f"upstream check runs"
        )

    call = opener or _urlopen_json
    url: str | None = (
        f"{api_root}/repos/{upstream_repo}/commits/{core_sha}/check-runs?per_page=100"
    )
    api_urls: list[str] = []
    check_runs: list[object] = []
    total_count: int | None = None

    while url is not None:
        api_urls.append(url)
        body, next_url = call(url, token)
        if not isinstance(body, Mapping):
            raise PromotionError(f"GitHub API response for {url} is not an object")
        page = body.get("check_runs")
        if not isinstance(page, list):
            raise PromotionError(
                f"GitHub API response for {url} has no 'check_runs' array"
            )
        if total_count is None:
            candidate = body.get("total_count")
            if not isinstance(candidate, int) or isinstance(candidate, bool):
                raise PromotionError(
                    f"GitHub API response for {url} has no integer 'total_count'"
                )
            total_count = candidate
        check_runs.extend(page)
        url = next_url
        if not page:
            break

    return {
        "total_count": total_count if total_count is not None else -1,
        "check_runs": check_runs,
        "api_urls": api_urls,
    }


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ci/promotion.py",
        description="Same-SHA core promotion verifier (fixture or live GitHub API mode).",
    )
    parser.add_argument("--repo", required=True, help="owner/name of the upstream core repo")
    parser.add_argument("--sha", required=True, help="the exact gitlink core SHA under test")
    parser.add_argument("--umbrella-sha", dest="umbrella_sha", default=None)
    parser.add_argument("--mode", choices=("fixture", "live"), required=True)
    parser.add_argument(
        "--fixture", default=None, help="path to a saved check-runs JSON body (fixture mode)"
    )
    parser.add_argument(
        "--token-env",
        dest="token_env",
        default=None,
        help="name (never value) of the env var holding the API token (live mode)",
    )
    parser.add_argument(
        "--required-check",
        dest="required_checks",
        action="append",
        default=None,
        help="a required check identity; repeatable. Defaults to the ci/gates.toml set.",
    )
    parser.add_argument(
        "--source-root",
        dest="source_root",
        default=None,
        help="root to load ci/gates.toml from when --required-check is not given",
    )
    return parser


def _resolve_required_checks(args: argparse.Namespace) -> tuple[str, ...]:
    if args.required_checks:
        return tuple(args.required_checks)
    if args.source_root:
        loaded = contract.load(Path(args.source_root) / "ci" / "gates.toml")
    else:
        loaded = contract.load()
    return required_core_checks(loaded)


def _scrub(text: str, token_env: str | None) -> str:
    if not token_env:
        return text
    value = os.environ.get(token_env)
    if value:
        text = text.replace(value, "***")
    return text


def main(argv: Sequence[str] | None = None, *, stdout=None, opener=None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    out = stdout if stdout is not None else sys.stdout

    try:
        required = _resolve_required_checks(args)
        if args.mode == "fixture":
            if not args.fixture:
                raise PromotionError("--mode fixture requires --fixture PATH")
            try:
                payload = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
            except OSError as exc:
                raise PromotionError(f"cannot read fixture {args.fixture}: {exc}") from exc
            except json.JSONDecodeError as exc:
                raise PromotionError(
                    f"fixture {args.fixture} is not valid JSON: {exc}"
                ) from exc
        else:
            payload = fetch_check_runs(
                args.repo, args.sha, token_env=args.token_env, opener=opener
            )
        result = verify_promotion(
            upstream_repo=args.repo,
            core_sha=args.sha,
            required_checks=required,
            payload=payload,
            umbrella_sha=args.umbrella_sha,
        )
    except (PromotionError, contract.ContractError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    text = json.dumps(result.evidence(), indent=2, sort_keys=True)
    print(_scrub(text, args.token_env), file=out)
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
