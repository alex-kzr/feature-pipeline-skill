# `ci/required_checks` fixtures (UGA-12)

Offline fixtures for
[`tests/test_ci_required_checks.py`](../../../test_ci_required_checks.py). No test in that
module performs a GitHub API call.

| Fixture | Used for |
| --- | --- |
| `acceptance-pass.json` | a complete, passing acceptance-evidence record; the failing shapes in the suite are in-memory mutations of this body |
| `check-runs-page-1.json`, `check-runs-page-2.json` | two `check-runs` API pages proving the read-only `fetch_check_runs` adapter follows pagination |

`acceptance-pass.json` carries one successful run per required-check identity in the current
`ci.promotion.required_check_contract`, separate producer (core SHA) and consumer/promotion
(umbrella SHA, gitlink = core SHA) bindings, executed gate-command steps, named dispatch refs,
raw policy pre-images, and post-apply rulesets equal to `render_desired_ruleset`.
