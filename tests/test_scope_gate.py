"""WT-02 — the deterministic allowed-scope gate.

After WT-01's bounded executor-window attribution, the runner still had no *structural*
response to an executor-owned change outside the task's ``Allowed scope``: attribution
merely labelled the path ``out_of_scope`` and ``dispatch_executor`` transitioned
``running -> implemented`` anyway, handing the decision to the LLM verifier (BL-02 finding
**F-04**; the CLI documents that an out-of-scope change is a blocking cause).

WT-02 turns that label into a mechanical application invariant
(:func:`feature_pipeline.application.execute_task.enforce_scope_gate`,
``docs/adr/007`` §6):

* the gate runs immediately after attribution and *before* any verifier launch;
* an executor-owned out-of-scope change yields a deterministic ``scope-violation`` decision
  whose :pyattr:`ScopeGateDecision.verifiers_may_launch` is ``False``;
* attribution that could not be trusted fails the gate closed (``attribution-unavailable``);
* **no file is reverted or cleaned** — the gate only ever writes bounded evidence, through
  the typed :class:`~feature_pipeline.ports.artifacts.ArtifactStorePort`, carrying explicit
  run and repository coordinates;
* a pre-existing dirty file the executor never touched is not a candidate and never trips
  the gate — WT-01 attribution already distinguishes it.

Standard library only.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from feature_pipeline.application.execute_task import (
    ATTRIBUTION_UNAVAILABLE,
    SCOPE_CLEAN,
    SCOPE_VIOLATION,
    ScopeEvidence,
    ScopeGateDecision,
    enforce_scope_gate,
)
from feature_pipeline.domain.errors import InvalidGlob
from feature_pipeline.domain.paths import RelativeGlob
from feature_pipeline.domain.scope import (
    IN_ALLOWED_SCOPE,
    OUT_OF_SCOPE,
    AllowedScope,
    ScopeViolation,
)
from feature_pipeline.infrastructure.artifacts.store import FileArtifactStore
from feature_pipeline.infrastructure.state.run_layout import allocate_run
from feature_pipeline.infrastructure.worktree.inspector import GitWorktreeInspector
from feature_pipeline.ports.artifacts import ArtifactRef
from feature_pipeline.ports.worktree import (
    CONFIDENCE_KNOWN,
    CONFIDENCE_KNOWN_EMPTY,
    CONFIDENCE_UNAVAILABLE,
    IN_SCOPE,
    OUT_OF_SCOPE as PORT_OUT_OF_SCOPE,
    BoundaryKind,
    CandidateChange,
    ChangeKind,
    RepositoryBoundary,
    WorktreeInspection,
)
from pipeline_core.worktree import _in_allowed_scope as _legacy_in_allowed_scope
from pipeline_core.worktree import _scope_regex as _legacy_scope_regex

_RUN_ID = "20260904T120000Z-abcdef01"


# --------------------------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------------------------


def _candidate(
    path: str,
    *,
    change: ChangeKind = ChangeKind.MODIFIED,
    classification: str = IN_SCOPE,
    digest: str | None = "sha256:00",
    boundary: str = "",
    is_symlink: bool = False,
) -> CandidateChange:
    return CandidateChange(
        path=path,
        change=change,
        classification=classification,
        digest=digest,
        is_symlink=is_symlink,
        boundary=boundary,
    )


def _inspection(
    candidates: tuple[CandidateChange, ...],
    *,
    available: bool = True,
    confidence: str = CONFIDENCE_KNOWN,
    reason: str | None = None,
    diff_text: str = "# attribution: known\n\n",
) -> WorktreeInspection:
    return WorktreeInspection(
        available=available,
        confidence=confidence,
        boundaries=(RepositoryBoundary(path="", kind=BoundaryKind.WORKING_ROOT),),
        candidates=candidates,
        diff_text=diff_text,
        reason=reason,
    )


def _store(tmp: Path) -> FileArtifactStore:
    layout = allocate_run(tmp / "storage", "demo", _RUN_ID)
    return FileArtifactStore(layout, fsync=False)


def _manifest_of(store: FileArtifactStore, ref: ArtifactRef) -> dict:
    root = store._layout.run_dir  # test-only reach-in
    return json.loads((root / ref.relative_path).read_text(encoding="utf-8"))


def _git(root: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=root, check=True, capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "core.autocrlf", "false")


# --------------------------------------------------------------------------------------------
# RelativeGlob.matches — the canonical scope matcher (glob matching was deferred to WT-02)
# --------------------------------------------------------------------------------------------


class RelativeGlobMatchingTests(unittest.TestCase):
    CASES = [
        ("src/feature_pipeline/domain/**", "src/feature_pipeline/domain/errors.py"),
        ("src/feature_pipeline/domain/**", "src/feature_pipeline/domain/sub/deep.py"),
        ("src/feature_pipeline/domain/**", "src/feature_pipeline/application/x.py"),
        ("src/feature_pipeline/domain/**", "src/feature_pipeline/domain"),
        ("tests/**", "tests/test_scope_gate.py"),
        ("tests/**", "tests"),
        ("a/*.py", "a/b.py"),
        ("a/*.py", "a/b/c.py"),
        ("docs/**/*.md", "docs/x/y.md"),
        ("docs/**/*.md", "docs/y.md"),
        ("*.md", "README.md"),
        ("*.md", "docs/README.md"),
        (
            "docs/plans/tasks/WT-02_deterministic-scope-gate.md",
            "docs/plans/tasks/WT-02_deterministic-scope-gate.md",
        ),
        (
            "docs/plans/tasks/WT-02_deterministic-scope-gate.md",
            "docs/plans/tasks/WT-01_candidate-worktree-attribution.md",
        ),
    ]

    def test_matches_agrees_with_the_legacy_scope_regex_byte_for_byte(self) -> None:
        for pattern, path in self.CASES:
            with self.subTest(pattern=pattern, path=path):
                legacy = path == pattern or bool(_legacy_scope_regex(pattern).match(path))
                self.assertEqual(RelativeGlob.parse(pattern).matches(path), legacy)

    def test_matches_accepts_a_relative_path_object(self) -> None:
        from feature_pipeline.domain.paths import RelativePath

        glob = RelativeGlob.parse("src/**")
        self.assertTrue(glob.matches(RelativePath.parse("src/a/b.py")))
        self.assertFalse(glob.matches(RelativePath.parse("tests/x.py")))

    def test_matches_normalizes_a_backslash_path_before_testing(self) -> None:
        self.assertTrue(
            RelativeGlob.parse("src/**").matches("src\\feature_pipeline\\domain\\x.py")
        )


# --------------------------------------------------------------------------------------------
# AllowedScope — domain classification
# --------------------------------------------------------------------------------------------


class AllowedScopeTests(unittest.TestCase):
    SCOPE = AllowedScope.parse(
        (
            "feature-pipeline-skill/src/feature_pipeline/application/execute_task.py",
            "feature-pipeline-skill/src/feature_pipeline/domain/**",
            "feature-pipeline-skill/tests/**",
            "docs/plans/tasks/WT-02_deterministic-scope-gate.md",
        )
    )

    def test_classify_in_and_out(self) -> None:
        self.assertEqual(
            self.SCOPE.classify(
                "feature-pipeline-skill/src/feature_pipeline/domain/scope.py"
            ),
            IN_ALLOWED_SCOPE,
        )
        self.assertEqual(
            self.SCOPE.classify(
                "feature-pipeline-skill/src/feature_pipeline/application/execute_task.py"
            ),
            IN_ALLOWED_SCOPE,
        )
        self.assertEqual(
            self.SCOPE.classify(
                "feature-pipeline-skill/src/feature_pipeline/infrastructure/worktree/inspector.py"
            ),
            OUT_OF_SCOPE,
        )
        self.assertEqual(self.SCOPE.classify("secret.py"), OUT_OF_SCOPE)

    def test_contains_is_classify_reduced_to_a_bool(self) -> None:
        self.assertTrue(
            self.SCOPE.contains("feature-pipeline-skill/tests/test_scope_gate.py")
        )
        self.assertFalse(self.SCOPE.contains("README.md"))

    def test_domain_labels_equal_the_port_and_legacy_labels(self) -> None:
        self.assertEqual((IN_ALLOWED_SCOPE, OUT_OF_SCOPE), (IN_SCOPE, PORT_OUT_OF_SCOPE))

    def test_empty_scope_classifies_everything_out(self) -> None:
        empty = AllowedScope.parse(())
        self.assertEqual(empty.classify("anything.py"), OUT_OF_SCOPE)

    def test_classification_matches_the_legacy_helper_over_a_sweep(self) -> None:
        raw = (
            "src/feature_pipeline/domain/**",
            "src/feature_pipeline/application/execute_task.py",
            "tests/**",
        )
        scope = AllowedScope.parse(raw)
        for path in [
            "src/feature_pipeline/domain/scope.py",
            "src/feature_pipeline/domain/a/b/c.py",
            "src/feature_pipeline/application/execute_task.py",
            "src/feature_pipeline/application/other.py",
            "tests/test_scope_gate.py",
            "src/feature_pipeline/infrastructure/x.py",
            "README.md",
        ]:
            with self.subTest(path=path):
                legacy = IN_ALLOWED_SCOPE if _legacy_in_allowed_scope(path, raw) else OUT_OF_SCOPE
                self.assertEqual(scope.classify(path), legacy)

    def test_absolute_scope_entry_fails_closed_at_parse(self) -> None:
        with self.assertRaises(InvalidGlob):
            AllowedScope.parse(("/etc/passwd",))
        with self.assertRaises(InvalidGlob):
            AllowedScope.parse(("../escape/**",))


# --------------------------------------------------------------------------------------------
# enforce_scope_gate — clean
# --------------------------------------------------------------------------------------------


class ScopeGateCleanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = _store(self.tmp)

    def test_all_in_scope_changes_pass_and_verifiers_may_launch(self) -> None:
        inspection = _inspection(
            (
                _candidate("src/feature_pipeline/domain/scope.py"),
                _candidate(
                    "src/feature_pipeline/application/execute_task.py",
                    change=ChangeKind.ADDED,
                ),
                _candidate("tests/test_scope_gate.py", change=ChangeKind.ADDED),
            )
        )
        decision = enforce_scope_gate(
            inspection,
            task_id="WT-02",
            run_id=_RUN_ID,
            allowed_scope=(
                "src/feature_pipeline/domain/**",
                "src/feature_pipeline/application/execute_task.py",
                "tests/**",
            ),
            store=self.store,
        )
        self.assertEqual(decision.outcome, SCOPE_CLEAN)
        self.assertTrue(decision.verifiers_may_launch)
        self.assertEqual(decision.violations, ())
        self.assertIsNone(decision.reason)

    def test_clean_decision_still_persists_bounded_evidence(self) -> None:
        decision = enforce_scope_gate(
            _inspection((_candidate("tests/x.py"),)),
            task_id="WT-02",
            run_id=_RUN_ID,
            allowed_scope=("tests/**",),
            store=self.store,
        )
        self.assertIsInstance(decision.evidence, ScopeEvidence)
        self.assertEqual(decision.evidence.manifest.kind, "report")
        self.assertTrue(decision.evidence.manifest.relative_path.startswith("reports/"))
        self.assertTrue(decision.evidence.diff.relative_path.startswith("reports/"))
        manifest = _manifest_of(self.store, decision.evidence.manifest)
        self.assertEqual(manifest["violations"], [])
        self.assertEqual(len(manifest["changed_files"]), 1)

    def test_known_empty_attribution_is_clean(self) -> None:
        decision = enforce_scope_gate(
            _inspection((), confidence=CONFIDENCE_KNOWN_EMPTY, diff_text="# attribution: known-empty\n"),
            task_id="WT-02",
            run_id=_RUN_ID,
            allowed_scope=("tests/**",),
            store=self.store,
        )
        self.assertEqual(decision.outcome, SCOPE_CLEAN)
        self.assertTrue(decision.verifiers_may_launch)


# --------------------------------------------------------------------------------------------
# enforce_scope_gate — violation
# --------------------------------------------------------------------------------------------


class ScopeGateViolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = _store(self.tmp)

    def _decide(self, inspection: WorktreeInspection) -> ScopeGateDecision:
        return enforce_scope_gate(
            inspection,
            task_id="WT-02",
            run_id=_RUN_ID,
            allowed_scope=("in_scope.py",),
            store=self.store,
        )

    def test_out_of_scope_change_blocks_before_verification(self) -> None:
        decision = self._decide(
            _inspection(
                (
                    _candidate("in_scope.py"),
                    _candidate("secret.py", classification=PORT_OUT_OF_SCOPE),
                )
            )
        )
        self.assertEqual(decision.outcome, SCOPE_VIOLATION)
        self.assertFalse(decision.verifiers_may_launch)
        self.assertEqual([v.path for v in decision.violations], ["secret.py"])
        self.assertIn("secret.py", decision.reason or "")
        self.assertIsInstance(decision.violations[0], ScopeViolation)

    def test_gate_re_derives_scope_and_does_not_trust_the_inspector_label(self) -> None:
        # The inspector mislabels an out-of-scope path as in-scope; the gate must still catch it.
        decision = self._decide(
            _inspection((_candidate("secret.py", classification=IN_SCOPE),))
        )
        self.assertEqual(decision.outcome, SCOPE_VIOLATION)
        self.assertEqual([v.path for v in decision.violations], ["secret.py"])

    def test_violation_manifest_records_every_candidate_and_the_violation_list(self) -> None:
        decision = self._decide(
            _inspection(
                (
                    _candidate("in_scope.py"),
                    _candidate(
                        "nested/secret.py",
                        change=ChangeKind.ADDED,
                        classification=PORT_OUT_OF_SCOPE,
                        boundary="",
                    ),
                )
            )
        )
        manifest = _manifest_of(self.store, decision.evidence.manifest)
        self.assertEqual(manifest["violations"], ["nested/secret.py"])
        classes = {row["path"]: row["classification"] for row in manifest["changed_files"]}
        self.assertEqual(classes["in_scope.py"], IN_ALLOWED_SCOPE)
        self.assertEqual(classes["nested/secret.py"], OUT_OF_SCOPE)
        # diff text is carried verbatim into the persisted diff artifact
        diff_ref = decision.evidence.diff
        diff_body = (self.store._layout.run_dir / diff_ref.relative_path).read_text(
            encoding="utf-8"
        )
        self.assertEqual(diff_body, "# attribution: known\n\n")

    def test_no_verifier_runs_once_the_gate_has_failed(self) -> None:
        launched: list[str] = []

        def _fake_verifier() -> None:
            launched.append("task-verifier")

        decision = self._decide(
            _inspection((_candidate("secret.py", classification=PORT_OUT_OF_SCOPE),))
        )
        if decision.verifiers_may_launch:  # pragma: no cover - must not happen
            _fake_verifier()
        self.assertEqual(launched, [])


# --------------------------------------------------------------------------------------------
# enforce_scope_gate — attribution unavailable
# --------------------------------------------------------------------------------------------


class ScopeGateUnavailableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = _store(self.tmp)

    def test_unavailable_attribution_fails_the_gate_closed(self) -> None:
        decision = enforce_scope_gate(
            _inspection(
                (),
                available=False,
                confidence=CONFIDENCE_UNAVAILABLE,
                reason="content of 'x' could not be read",
                diff_text="# attribution: unavailable\n# reason: content of 'x' could not be read\n",
            ),
            task_id="WT-02",
            run_id=_RUN_ID,
            allowed_scope=("tests/**",),
            store=self.store,
        )
        self.assertEqual(decision.outcome, ATTRIBUTION_UNAVAILABLE)
        self.assertFalse(decision.verifiers_may_launch)
        self.assertIn("could not be read", decision.reason or "")

    def test_unavailable_still_persists_the_diagnostic_diff(self) -> None:
        decision = enforce_scope_gate(
            _inspection(
                (),
                available=False,
                confidence=CONFIDENCE_UNAVAILABLE,
                reason="no Git repository boundary at the working root",
                diff_text="# attribution: unavailable\n# reason: no boundary\n",
            ),
            task_id="WT-02",
            run_id=_RUN_ID,
            allowed_scope=("tests/**",),
            store=self.store,
        )
        body = (self.store._layout.run_dir / decision.evidence.diff.relative_path).read_text(
            encoding="utf-8"
        )
        self.assertIn("unavailable", body)
        manifest = _manifest_of(self.store, decision.evidence.manifest)
        self.assertFalse(manifest["attribution_available"])
        self.assertEqual(manifest["attribution_confidence"], CONFIDENCE_UNAVAILABLE)


# --------------------------------------------------------------------------------------------
# AC-3 — evidence is bounded and coordinate-safe
# --------------------------------------------------------------------------------------------


class ScopeEvidenceCoordinateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = _store(self.tmp)

    def test_manifest_carries_explicit_run_and_repository_coordinates(self) -> None:
        decision = enforce_scope_gate(
            _inspection((_candidate("tests/x.py"),)),
            task_id="WT-02",
            run_id=_RUN_ID,
            allowed_scope=("tests/**",),
            store=self.store,
            repository="<repo_root>",
        )
        manifest = _manifest_of(self.store, decision.evidence.manifest)
        self.assertEqual(manifest["run_id"], _RUN_ID)
        self.assertEqual(manifest["repository"], "<repo_root>")
        self.assertEqual(manifest["task_id"], "WT-02")
        self.assertEqual(decision.evidence.run_id, _RUN_ID)
        self.assertEqual(decision.evidence.repository, "<repo_root>")

    def test_evidence_paths_are_run_relative_posix_never_absolute(self) -> None:
        decision = enforce_scope_gate(
            _inspection((_candidate("tests/x.py"),)),
            task_id="WT-02",
            run_id=_RUN_ID,
            allowed_scope=("tests/**",),
            store=self.store,
        )
        for ref in (decision.evidence.manifest, decision.evidence.diff):
            self.assertFalse(Path(ref.relative_path).is_absolute())
            self.assertNotIn("\\", ref.relative_path)
            self.assertNotIn("..", ref.relative_path.split("/"))

    def test_manifest_serialization_is_deterministic(self) -> None:
        inspection = _inspection(
            (
                _candidate("tests/b.py"),
                _candidate("tests/a.py"),
                _candidate("secret.py", classification=PORT_OUT_OF_SCOPE),
            )
        )
        first = enforce_scope_gate(
            inspection, task_id="WT-02", run_id=_RUN_ID,
            allowed_scope=("tests/**",), store=_store(Path(tempfile.mkdtemp())),
        )
        second = enforce_scope_gate(
            inspection, task_id="WT-02", run_id=_RUN_ID,
            allowed_scope=("tests/**",), store=_store(Path(tempfile.mkdtemp())),
        )
        self.assertEqual(first.evidence.manifest, second.evidence.manifest)


# --------------------------------------------------------------------------------------------
# AC-2 — no file is reverted, and pre-existing dirt is not misattributed
# --------------------------------------------------------------------------------------------


class NoRevertTests(unittest.TestCase):
    def test_gate_never_touches_the_worktree(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        repo = tmp / "repo"
        repo.mkdir()
        _init_repo(repo)
        (repo / "in_scope.py").write_text("start\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")

        inspector = GitWorktreeInspector()
        marker = inspector.capture_marker(str(repo))
        (repo / "in_scope.py").write_text("touched by executor\n", encoding="utf-8")
        (repo / "secret.py").write_text("written outside scope\n", encoding="utf-8")
        inspection = inspector.attribute(
            marker, str(repo), allowed_scope=["in_scope.py"]
        )

        store = _store(tmp)
        decision = enforce_scope_gate(
            inspection,
            task_id="WT-02",
            run_id=_RUN_ID,
            allowed_scope=("in_scope.py",),
            store=store,
        )

        self.assertEqual(decision.outcome, SCOPE_VIOLATION)
        # Nothing reverted: both files keep the executor's bytes and still exist.
        self.assertEqual(
            (repo / "in_scope.py").read_text(encoding="utf-8"), "touched by executor\n"
        )
        self.assertEqual(
            (repo / "secret.py").read_text(encoding="utf-8"), "written outside scope\n"
        )
        # git status is unchanged by the gate.
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo, check=True, capture_output=True, text=True,
        ).stdout
        self.assertIn("secret.py", status)
        self.assertIn("in_scope.py", status)

    def test_pre_existing_dirty_out_of_scope_file_does_not_trip_the_gate(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        repo = tmp / "repo"
        repo.mkdir()
        _init_repo(repo)
        (repo / "in_scope.py").write_text("start\n", encoding="utf-8")
        (repo / "user_notes.txt").write_text("v1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")

        # The user leaves an out-of-scope file dirty *before* the window opens.
        (repo / "user_notes.txt").write_text("user edit before the run\n", encoding="utf-8")

        inspector = GitWorktreeInspector()
        marker = inspector.capture_marker(str(repo))
        # The executor only changes an in-scope file.
        (repo / "in_scope.py").write_text("touched\n", encoding="utf-8")
        inspection = inspector.attribute(
            marker, str(repo), allowed_scope=["in_scope.py"]
        )

        decision = enforce_scope_gate(
            inspection,
            task_id="WT-02",
            run_id=_RUN_ID,
            allowed_scope=("in_scope.py",),
            store=_store(tmp),
        )
        self.assertEqual(decision.outcome, SCOPE_CLEAN)
        self.assertTrue(decision.verifiers_may_launch)
        self.assertEqual(
            (repo / "user_notes.txt").read_text(encoding="utf-8"),
            "user edit before the run\n",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
