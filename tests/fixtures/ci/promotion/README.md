# UGA-07 promotion verifier fixtures

Static, offline GitHub *check-runs* API responses for `ci/promotion.py`. Each file is the
JSON body GitHub returns for
`GET /repos/{owner}/feature-pipeline-skill/commits/{sha}/check-runs` (plus a synthetic
`api_urls` array naming the requests that produced it, so redacted evidence can cite the
URLs without a live call).

The synthetic required check identities used here are `lint-and-types`, `coverage`,
`suite-a`, and `suite-b`. The exact core SHA under test is
`1111111111111111111111111111111111111111`; `2222222222222222222222222222222222222222`
is an unrelated SHA.

| File | Shape it pins |
|---|---|
| `success.json` | every required check completed `success` for the exact SHA |
| `wrong-sha.json` | every required check is green, but for a different `head_sha` |
| `pending.json` | one required check is still `in_progress` |
| `failing.json` | one required check completed with `failure` |
| `incomplete-pagination.json` | `total_count` exceeds the number of returned runs |
| `malformed-not-object.json` | the response is a JSON array, not an object |
| `duplicate-reruns.json` | an early `failure` superseded by a later `success` rerun |
