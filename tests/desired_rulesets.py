"""UGA-13 - the versioned desired remote-enforcement ruleset snapshot.

``ci/desired_rulesets.json`` is a reviewed, fully offline snapshot of the exact managed branch
rulesets an operator must apply in UGA-14. It is rendered here from:

* the UGA-10 required-check identity contract
  (:func:`ci.promotion.required_check_contract`), the *only* source of check identities, and
* the UGA-12 pure renderer
  (:func:`ci.required_checks.render_desired_ruleset`), plus
* the explicit, project-owned :data:`TARGETS` below (repository, role, default branch, managed
  ruleset name, enforcement mode and the check-provider binding).

:mod:`tests.test_desired_rulesets` diffs the checked-in file against a fresh render and fails
on any drift, so a contract change that is not mirrored in the snapshot is a red test rather
than silent divergence.

Regenerate after a *reviewed* contract change, from ``feature-pipeline-skill/``::

    uv run python -m tests.desired_rulesets --write

``--write`` also refreshes the generated JSON block in
``docs/validation/github-actions-remote-enforcement-plan.md``. It prepares files for review
only: it makes no GitHub API call, mutates no ruleset, and authorizes no commit.

Standard library only. The installable runtime never imports this module.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ci import contract
from ci import promotion
from ci import required_checks as rc

#: ``feature-pipeline-skill/ci/desired_rulesets.json``.
SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "ci" / "desired_rulesets.json"

#: The remote-enforcement plan page whose generated JSON block mirrors the snapshot.
PLAN_DOC_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "validation"
    / "github-actions-remote-enforcement-plan.md"
)

_DOC_BEGIN = "<!-- BEGIN GENERATED desired_rulesets.json -->"
_DOC_END = "<!-- END GENERATED desired_rulesets.json -->"

#: GitHub-hosted "GitHub Actions" check-provider app id. This is an *assumption* UGA-14 must
#: confirm read-only against each repository before applying any ruleset (see the plan page).
GITHUB_ACTIONS_APP_ID = 15368

#: Explicit target inputs. None of these are derived from the identity-only contract; the
#: operator confirms each against the live repository in UGA-14 before applying.
TARGETS: tuple[rc.RulesetTarget, ...] = (
    rc.RulesetTarget(
        repository="alex-kzr/feature-pipeline-skill",
        role="producer",
        default_branch="main",
        ruleset_name="core-required-checks",
        enforcement="active",
        check_provider_id=GITHUB_ACTIONS_APP_ID,
    ),
    rc.RulesetTarget(
        repository="alex-kzr/feature-pipeline",
        role="consumer",
        default_branch="main",
        ruleset_name="umbrella-required-checks",
        enforcement="active",
        check_provider_id=GITHUB_ACTIONS_APP_ID,
    ),
)

_REGEN = "uv run python -m tests.desired_rulesets --write  # cwd: feature-pipeline-skill"


def _target_input(target: rc.RulesetTarget) -> dict[str, object]:
    return {
        "repository": target.repository,
        "role": target.role,
        "default_branch": target.default_branch,
        "ruleset_name": target.ruleset_name,
        "enforcement": target.enforcement,
        "check_provider_id": target.check_provider_id,
    }


def render_snapshot(loaded: contract.Contract | None = None) -> dict[str, object]:
    """Render the full snapshot: explicit target inputs plus one payload per role.

    Pure: a function of ``ci/gates.toml`` (via the identity contract) and :data:`TARGETS`.
    Never calls GitHub and never reads workflow YAML.
    """

    loaded = loaded if loaded is not None else contract.load()
    rcc = promotion.required_check_contract(loaded)
    snapshot: dict[str, object] = {
        "schema": 1,
        "note": f"UGA-13 offline snapshot; regenerate with: {_REGEN}",
        "targets": [_target_input(target) for target in TARGETS],
    }
    for target in TARGETS:
        snapshot[target.role] = rc.render_desired_ruleset(rcc, target)
    return snapshot


def serialize(snapshot: dict[str, object]) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _patch_plan_doc(block_json: str) -> bool:
    if not PLAN_DOC_PATH.exists():
        return False
    doc = PLAN_DOC_PATH.read_text(encoding="utf-8")
    before, begin, rest = doc.partition(_DOC_BEGIN)
    _, end, after = rest.partition(_DOC_END)
    if not begin or not end:
        return False
    replacement = f"{_DOC_BEGIN}\n\n```json\n{block_json.rstrip()}\n```\n\n{_DOC_END}"
    updated = before + replacement + after
    if updated != doc:
        PLAN_DOC_PATH.write_text(updated, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tests.desired_rulesets")
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite ci/desired_rulesets.json (and the plan-doc JSON block) in place",
    )
    args = parser.parse_args(argv)

    text = serialize(render_snapshot())
    if args.write:
        SNAPSHOT_PATH.write_text(text, encoding="utf-8")
        patched = _patch_plan_doc(text)
        print(f"wrote {SNAPSHOT_PATH}")
        print("patched plan-doc JSON block" if patched else "plan-doc JSON block not found")
        return 0

    current = SNAPSHOT_PATH.read_text(encoding="utf-8") if SNAPSHOT_PATH.exists() else ""
    if current != text:
        print(
            "ci/desired_rulesets.json is stale; regenerate with: " + _REGEN,
            file=sys.stderr,
        )
        return 1
    print("ci/desired_rulesets.json is up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
