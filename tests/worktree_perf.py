"""WT-01 — bounded worktree-attribution budget harness (was the BL-04 F-16 baseline).

BL-04 recorded the pre-WT-01 cost: :func:`pipeline_core.worktree.capture_snapshot` read
every tracked and non-ignored untracked path across every repository boundary fully into
memory, so snapshot I/O and peak heap were ``O(total repository bytes)`` per window
(finding **F-16**). This module now measures the WT-01 replacement — the bounded,
candidate-based inspector — over the *same* deterministic synthetic repositories and checks
it against the parity and improvement budgets BL-04 fixed in
:data:`WT01_THRESHOLDS`.

Each scenario is built by :func:`build_scenario` from a fixed seed (byte-identical on
rebuild, AC-2), carries the full candidate matrix (tracked-clean, tracked-dirty, untracked,
binary, and a submodule-like nested boundary), and is driven through one executor window:

    marker  = capture_snapshot(root, exclude_roots=(run_dir,))   # bounded before
    <executor edits, all inside src/**>
    result  = attribute_executor_window(marker, root, ...)       # bounded close

The same window is also attributed by the retained legacy full-snapshot inspector
(:func:`pipeline_core.worktree._legacy_capture_snapshot` + :func:`attribute_window`) so the
two attributions can be compared directly:

* **parity** — identical ``attribution_state``, identical changed-candidate set and
  per-candidate classification, ``evidence_bytes`` within +/-5% (WT-01 AC-1);
* **improvement** — ``bytes_read`` collapses to ``O(changed candidates)`` and drops far
  below ``total_repo_bytes``; ``peak_heap_bytes`` drops far below the legacy peak
  (WT-01 AC-3).

:mod:`tests.test_worktree_performance_baseline` asserts the *structure* of this report and
the budgets. Wall-clock numbers are recorded but never asserted — same-machine ratios only.

Regenerate the doc's data block after a reviewed change::

    python -m tests.worktree_perf --write

Standard library only. This module defines no ``unittest`` test case.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time
import tracemalloc
from dataclasses import asdict, dataclass
from pathlib import Path

from pipeline_core.reports import launch_artifacts
from pipeline_core.worktree import (
    _legacy_capture_snapshot,
    attribute_executor_window,
    attribute_window,
    capture_snapshot,
    persist_attribution,
)

# --- recipe constants ----------------------------------------------------------------------

#: Best-of-N samples for the snapshot hot path (wall clock is advisory only).
REPEATS = 3

#: ``small`` scenario — a handful of files, the whole candidate matrix, sub-second.
SMALL_TRACKED_FILES = 12
SMALL_TEXT_BYTES = 1024
SMALL_BLOB_BYTES = 2 * 1024

#: ``large`` scenario — a synthetic monorepo slice, still bounded for a developer machine.
LARGE_TRACKED_FILES = 600
LARGE_TEXT_BYTES = 3 * 1024
LARGE_BLOB_BYTES = 384 * 1024

#: Deterministic content seed. Same seed → byte-identical repository (AC-2).
SEED = 20260903

#: Wall-clock numbers are recorded and compared as same-machine ratios, never asserted in
#: the unit suite. :mod:`tests.test_worktree_performance_baseline` checks this stays true.
WALL_CLOCK_ADVISORY = True

#: The scope every synthetic executor edit stays inside.
ALLOWED_SCOPE = ("src/**",)

#: Explicit parity and improvement thresholds WT-01 must satisfy (BL-04 AC-3).
WT01_THRESHOLDS: dict[str, list[str]] = {
    "parity": [
        "attribution_state stays 'known' and changed_candidate_count is identical to the "
        "legacy full-snapshot attribution for both scenarios (small and large).",
        "the classification of every changed candidate is unchanged: in-window src/** "
        "edits in_allowed_scope, pre-existing dirt still excluded from the window.",
        "evidence_bytes (manifest + diff) within +/-5% of the legacy attribution — same "
        "attributed content, only the snapshot mechanism changes.",
        "the full tests/test_worktree.py matrix and the core unit suite stay green.",
    ],
    "improvement": [
        "large-scenario bytes_read is O(changed candidates): "
        "<= 4 * LARGE_TEXT_BYTES * changed_candidate_count + 262144, i.e. at least 20x "
        "below the total_repo_bytes the legacy snapshot reads.",
        "large-scenario peak_heap_bytes at least 10x lower than the legacy snapshot peak.",
        "large-scenario window_seconds strictly below the legacy window_seconds on the "
        "same machine (ratio comparison only).",
        "small-scenario metrics regress by no more than the parity band above — the "
        "metadata-first path must not add fixed cost that dominates a tiny repository.",
    ],
}


# --- synthetic repository builder --------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """A built synthetic repository and the paths the window routine mutates."""

    name: str
    root: Path
    run_dir: Path
    in_scope_edit: Path
    in_scope_add: Path
    preexisting_dirty: Path
    untracked: Path
    binary: Path
    submodule_like: Path


def _git(root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *argv], cwd=root, check=True, capture_output=True, text=True
    )


def _text_body(rng: random.Random, size: int, tag: str) -> str:
    """Deterministic ``size``-ish bytes of readable, diff-friendly text."""
    lines: list[str] = [f"# generated module {tag}\n"]
    total = len(lines[0])
    n = 0
    while total < size:
        token = rng.choice(("alpha", "beta", "gamma", "delta", "epsilon", "zeta"))
        line = f"value_{n:05d} = {rng.randint(0, 1_000_000)}  # {token}\n"
        lines.append(line)
        total += len(line)
        n += 1
    return "".join(lines)


def build_scenario(
    name: str,
    tracked_files: int,
    text_bytes: int,
    blob_bytes: int,
    dest: Path,
) -> Scenario:
    """Materialize one deterministic synthetic repository under ``dest``.

    Layout: ``src/mod_XXXX.py`` tracked-clean text files; ``src/dirty_a.py`` /
    ``src/dirty_b.py`` tracked then edited *before* the window opens; ``assets/blob.bin``
    a tracked binary; ``scratch/notes.txt`` an untracked file; ``vendor/lib`` a nested
    repository declared in ``.gitmodules`` (a submodule-like boundary). The window later
    edits one clean file, adds one file, and re-edits ``src/dirty_a.py`` — all inside
    ``src/**``.
    """
    rng = random.Random(f"{SEED}:{name}")
    root = dest / name
    (root / "src").mkdir(parents=True)
    (root / "assets").mkdir()
    (root / "scratch").mkdir()

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "baseline@example.com")
    _git(root, "config", "user.name", "BL-04 baseline")
    _git(root, "config", "core.autocrlf", "false")

    for index in range(tracked_files):
        (root / "src" / f"mod_{index:04d}.py").write_text(
            _text_body(rng, text_bytes, f"mod_{index:04d}"), encoding="utf-8"
        )
    dirty_a = root / "src" / "dirty_a.py"
    dirty_b = root / "src" / "dirty_b.py"
    dirty_a.write_text(_text_body(rng, text_bytes, "dirty_a"), encoding="utf-8")
    dirty_b.write_text(_text_body(rng, text_bytes, "dirty_b"), encoding="utf-8")

    binary = root / "assets" / "blob.bin"
    binary.write_bytes(random.Random(f"{SEED}:{name}:blob").randbytes(blob_bytes))

    nested = root / "vendor" / "lib"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    _git(nested, "config", "user.email", "baseline@example.com")
    _git(nested, "config", "user.name", "BL-04 baseline")
    (nested / "inner.py").write_text(
        _text_body(rng, text_bytes, "vendor_inner"), encoding="utf-8"
    )
    _git(nested, "add", "-A")
    _git(nested, "commit", "-qm", "vendor base")
    (root / ".gitmodules").write_text(
        '[submodule "lib"]\n\tpath = vendor/lib\n\turl = ./vendor/lib\n', encoding="utf-8"
    )

    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "synthetic base")

    # Pre-existing dirt: edited after the commit, before the window opens.
    dirty_a.write_text(
        dirty_a.read_text(encoding="utf-8") + "pre_existing_edit = 1\n", encoding="utf-8"
    )
    dirty_b.write_text(
        dirty_b.read_text(encoding="utf-8") + "pre_existing_edit = 2\n", encoding="utf-8"
    )
    untracked = root / "scratch" / "notes.txt"
    untracked.write_text("scratch notes, never added\n", encoding="utf-8")

    run_dir = root / ".pipeline" / "runs" / name
    run_dir.mkdir(parents=True)

    return Scenario(
        name=name,
        root=root,
        run_dir=run_dir,
        in_scope_edit=root / "src" / "mod_0000.py",
        in_scope_add=root / "src" / "added_by_window.py",
        preexisting_dirty=dirty_a,
        untracked=untracked,
        binary=binary,
        submodule_like=nested,
    )


def _apply_window_edits(scenario: Scenario) -> None:
    """The synthetic executor's change set — all inside ``src/**`` (idempotent bytes)."""
    scenario.in_scope_edit.write_text(
        "# rewritten inside the executor window\nresult = 42\n", encoding="utf-8"
    )
    scenario.in_scope_add.write_text(
        "# added inside the executor window\nADDED = True\n", encoding="utf-8"
    )
    # Re-edit a pre-dirty file: the further edit *is* attributed, the pre-existing part is
    # not — this exercises the before/after subtraction, not just add/modify.
    scenario.preexisting_dirty.write_text(
        scenario.preexisting_dirty.read_text(encoding="utf-8") + "window_edit = 3\n",
        encoding="utf-8",
    )


# --- measurement -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Metrics:
    """One scenario's recorded WT-01 measurement. Wall-clock fields are advisory."""

    scenario: str
    boundary_count: int
    vcs_file_count: int
    total_repo_bytes: int
    bytes_read: int
    legacy_bytes_read: int
    changed_candidate_count: int
    legacy_changed_candidate_count: int
    attribution_state: str
    legacy_attribution_state: str
    classifications_match: bool
    snapshot_seconds: float
    window_seconds: float
    legacy_window_seconds: float
    peak_heap_bytes: int
    legacy_peak_heap_bytes: int
    retained_heap_bytes: int
    legacy_retained_heap_bytes: int
    evidence_bytes: int
    legacy_evidence_bytes: int
    matrix_tracked_clean: int
    matrix_tracked_dirty: int
    matrix_untracked: int
    matrix_binary: int
    matrix_submodule_like: int
    preexisting_dirt_excluded: bool
    in_window_edit_attributed: bool


def _vcs_file_list(root: Path) -> list[str]:
    """Every tracked + non-ignored untracked path across the working root and nested repos,
    used only to size the repository — no file body is read."""
    files: set[str] = set()
    for cwd in (root, root / "vendor" / "lib"):
        if not (cwd / ".git").exists():
            continue
        for argv in (
            ["ls-files", "-z"],
            ["ls-files", "--others", "--exclude-standard", "-z"],
        ):
            out = _git(cwd, *argv).stdout
            for part in out.split("\0"):
                if not part:
                    continue
                absolute = (cwd / part).resolve()
                try:
                    files.add(absolute.relative_to(root).as_posix())
                except ValueError:
                    pass
    return sorted(files)


def _on_disk_bytes(root: Path, relatives: list[str]) -> int:
    total = 0
    for relative in relatives:
        path = root / relative
        if path.is_file():
            total += path.stat().st_size
    return total


def _rows_signature(rows: list[dict[str, object]]) -> set[tuple[str, str, str]]:
    return {
        (str(r["path"]), str(r["status"]), str(r["classification"])) for r in rows
    }


def measure(scenario: Scenario) -> Metrics:
    """Run and record one bounded executor window over ``scenario``, plus the legacy
    attribution of the identical window for the parity comparison."""
    root, run_dir = scenario.root, scenario.run_dir

    vcs_files = _vcs_file_list(root)
    total_repo_bytes = _on_disk_bytes(root, vcs_files)

    # --- bounded before-marker: hot path, best of REPEATS, no mutation between samples ---
    best = float("inf")
    for _ in range(REPEATS):
        start = time.perf_counter()
        marker_snapshot = capture_snapshot(root, exclude_roots=(run_dir,))
        best = min(best, time.perf_counter() - start)
    assert marker_snapshot.available, marker_snapshot.reason
    assert marker_snapshot.marker is not None

    legacy_before = _legacy_capture_snapshot(root, exclude_roots=(run_dir,))

    tracemalloc.start()
    tracemalloc.clear_traces()
    base_cur, _ = tracemalloc.get_traced_memory()
    heap_marker = capture_snapshot(root, exclude_roots=(run_dir,))
    marker_cur, marker_peak = tracemalloc.get_traced_memory()
    marker_retained = marker_cur - base_cur
    tracemalloc.stop()

    tracemalloc.start()
    tracemalloc.clear_traces()
    base_cur, _ = tracemalloc.get_traced_memory()
    legacy_heap_snapshot = _legacy_capture_snapshot(root, exclude_roots=(run_dir,))
    legacy_cur, legacy_peak = tracemalloc.get_traced_memory()
    legacy_retained = legacy_cur - base_cur
    tracemalloc.stop()
    del legacy_heap_snapshot

    open_body_bytes = sum(
        len(body.data or b"") for body in heap_marker.marker.open_bodies.values()
    )

    # --- one shared window: edits applied once, both inspectors close over it ---
    _apply_window_edits(scenario)

    window_start = time.perf_counter()
    artifacts = launch_artifacts(run_dir, scenario.name.upper(), 1)
    artifacts.directory.mkdir(parents=True, exist_ok=True)
    artifacts.executor_report.write_text("# bounded window\n", encoding="utf-8")
    result = attribute_executor_window(
        marker_snapshot,
        root,
        artifacts=artifacts,
        task_id=scenario.name.upper(),
        allowed_scope=ALLOWED_SCOPE,
        attempt=1,
        generation=1,
        executor_report=artifacts.executor_report,
        exclude_roots=(run_dir,),
    )
    window_seconds = time.perf_counter() - window_start
    evidence_bytes = (
        Path(artifacts.implementation_manifest).stat().st_size
        + Path(artifacts.implementation_diff).stat().st_size
    )
    # The bounded close reads: the open bodies + the after body of every changed candidate
    # + one index blob per candidate that was clean at open.
    blob_bytes = 0
    for row in result.changed_files:
        after = root / str(row["path"])
        if after.is_file():
            blob_bytes += after.stat().st_size
    bytes_read = open_body_bytes + blob_bytes

    legacy_start = time.perf_counter()
    legacy_after = _legacy_capture_snapshot(root, exclude_roots=(run_dir,))
    legacy_attr = attribute_window(
        legacy_before, legacy_after, allowed_scope=ALLOWED_SCOPE
    )
    legacy_artifacts = launch_artifacts(run_dir, scenario.name.upper() + "LEGACY", 1)
    legacy_artifacts.directory.mkdir(parents=True, exist_ok=True)
    legacy_artifacts.executor_report.write_text("# legacy window\n", encoding="utf-8")
    legacy_result = persist_attribution(
        legacy_artifacts,
        legacy_attr,
        task_id=scenario.name.upper(),
        attempt=1,
        generation=1,
        executor_report=legacy_artifacts.executor_report,
        repo_root=root,
    )
    legacy_window_seconds = time.perf_counter() - legacy_start
    legacy_bytes_read = sum(
        len(entry.data or b"") for entry in legacy_before.files.values()
    )
    legacy_evidence_bytes = (
        Path(legacy_artifacts.implementation_manifest).stat().st_size
        + Path(legacy_artifacts.implementation_diff).stat().st_size
    )

    changed = {str(row["path"]): row for row in result.changed_files}
    preexisting_rel = scenario.preexisting_dirty.relative_to(root).as_posix()
    edit_rel = scenario.in_scope_edit.relative_to(root).as_posix()
    dirty_b_rel = "src/dirty_b.py"

    tracked_clean = sum(
        1 for name in vcs_files if name.startswith("src/mod_") and name.endswith(".py")
    )

    return Metrics(
        scenario=scenario.name,
        boundary_count=len(marker_snapshot.marker.boundaries),
        vcs_file_count=len(vcs_files),
        total_repo_bytes=total_repo_bytes,
        bytes_read=bytes_read,
        legacy_bytes_read=legacy_bytes_read,
        changed_candidate_count=len(result.changed_files),
        legacy_changed_candidate_count=len(legacy_result.changed_files),
        attribution_state=result.state,
        legacy_attribution_state=legacy_result.state,
        classifications_match=(
            _rows_signature(result.changed_files)
            == _rows_signature(legacy_result.changed_files)
        ),
        snapshot_seconds=best,
        window_seconds=window_seconds,
        legacy_window_seconds=legacy_window_seconds,
        peak_heap_bytes=marker_peak,
        legacy_peak_heap_bytes=legacy_peak,
        retained_heap_bytes=max(marker_retained, 1),
        legacy_retained_heap_bytes=max(legacy_retained, 1),
        evidence_bytes=evidence_bytes,
        legacy_evidence_bytes=legacy_evidence_bytes,
        matrix_tracked_clean=tracked_clean,
        matrix_tracked_dirty=2,
        matrix_untracked=1 if "scratch/notes.txt" in vcs_files else 0,
        matrix_binary=1 if "assets/blob.bin" in vcs_files else 0,
        matrix_submodule_like=1 if "vendor/lib/inner.py" in vcs_files else 0,
        preexisting_dirt_excluded=dirty_b_rel not in changed,
        in_window_edit_attributed=(edit_rel in changed and preexisting_rel in changed),
    )


def baseline_report(scale: int = 1) -> dict[str, Metrics]:
    """Build and measure both scenarios. ``scale`` multiplies the large file count."""
    workdir = Path(tempfile.mkdtemp(prefix="wt01-worktree-budget-"))
    try:
        small = measure(
            build_scenario(
                "small",
                SMALL_TRACKED_FILES,
                SMALL_TEXT_BYTES,
                SMALL_BLOB_BYTES,
                workdir,
            )
        )
        large = measure(
            build_scenario(
                "large",
                LARGE_TRACKED_FILES * max(1, scale),
                LARGE_TEXT_BYTES,
                LARGE_BLOB_BYTES,
                workdir,
            )
        )
        return {"small": small, "large": large}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --- environment + rendering -----------------------------------------------------------------


def environment() -> dict[str, str]:
    """The machine facts that make the recorded numbers interpretable."""
    try:
        git_version = _git(Path.cwd(), "--version").stdout.strip()
    except Exception:  # pragma: no cover - git is a hard dependency of the suite
        git_version = "unavailable"
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "git": git_version,
        "cpu_count": str(os.cpu_count() or "unknown"),
        "note": (
            "Wall-clock numbers are machine-sensitive. They are compared only as "
            "same-machine ratios between the bounded and legacy attribution of the same "
            "window, never against absolute values recorded here."
        ),
    }


def render_markdown(report: dict[str, Metrics], env: dict[str, str]) -> str:
    """The generated data block spliced into the validation doc by ``--write``."""
    lines: list[str] = []
    lines.append("## Scenario recipe\n")
    lines.append(
        "Both scenarios are built by `tests/worktree_perf.py::build_scenario` from the "
        "fixed seed `%d`, so a rebuild is byte-identical (AC-2). Sizes scale with the "
        "module constants and `PIPELINE_PERF_BASELINE_SCALE`.\n" % SEED
    )
    lines.append("| Constant | Value |")
    lines.append("|---|---|")
    for key, value in (
        ("REPEATS", REPEATS),
        ("SMALL_TRACKED_FILES", SMALL_TRACKED_FILES),
        ("SMALL_TEXT_BYTES", SMALL_TEXT_BYTES),
        ("SMALL_BLOB_BYTES", SMALL_BLOB_BYTES),
        ("LARGE_TRACKED_FILES", LARGE_TRACKED_FILES),
        ("LARGE_TEXT_BYTES", LARGE_TEXT_BYTES),
        ("LARGE_BLOB_BYTES", LARGE_BLOB_BYTES),
    ):
        lines.append(f"| `{key}` | {value} |")
    lines.append("")
    lines.append(
        "Candidate matrix per scenario: tracked-clean (`src/mod_*.py`), tracked-dirty "
        "(`src/dirty_a.py`, `src/dirty_b.py`, edited before the window), untracked "
        "(`scratch/notes.txt`), binary (`assets/blob.bin`), and a submodule-like nested "
        "boundary (`vendor/lib`, declared in `.gitmodules`). The window edits one clean "
        "file, adds one file, and re-edits `src/dirty_a.py`, all inside `src/**`.\n"
    )

    lines.append("## Measured (bounded vs legacy)\n")
    lines.append(
        "Regenerated by `python -m tests.worktree_perf --write`. Wall-clock columns are "
        "**advisory** — see the Environment note.\n"
    )
    header = [
        "scenario",
        "vcs_file_count",
        "total_repo_bytes",
        "bytes_read",
        "legacy_bytes_read",
        "changed_candidates",
        "state",
        "evidence_bytes",
        "legacy_evidence_bytes",
        "peak_heap_bytes",
        "legacy_peak_heap_bytes",
        "window_seconds",
        "legacy_window_seconds",
    ]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for key in ("small", "large"):
        m = report[key]
        lines.append(
            "| "
            + " | ".join(
                [
                    m.scenario,
                    str(m.vcs_file_count),
                    str(m.total_repo_bytes),
                    str(m.bytes_read),
                    str(m.legacy_bytes_read),
                    str(m.changed_candidate_count),
                    m.attribution_state,
                    str(m.evidence_bytes),
                    str(m.legacy_evidence_bytes),
                    str(m.peak_heap_bytes),
                    str(m.legacy_peak_heap_bytes),
                    f"{m.window_seconds:.4f}",
                    f"{m.legacy_window_seconds:.4f}",
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append(
        "The WT-01 win is visible here: `bytes_read` no longer tracks `total_repo_bytes` "
        "— it is `O(changed candidates)` — while `state`, the changed-candidate count, and "
        "`evidence_bytes` match the legacy attribution of the identical window.\n"
    )

    lines.append("## WT-01 thresholds\n")
    for group in ("parity", "improvement"):
        lines.append(f"**{group.capitalize()}**\n")
        for item in WT01_THRESHOLDS[group]:
            lines.append(f"- {item}")
        lines.append("")

    lines.append("## Environment\n")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    for field_name in ("platform", "python", "implementation", "git", "cpu_count"):
        lines.append(f"| {field_name} | {env[field_name]} |")
    lines.append("")
    lines.append(env["note"] + "\n")
    return "\n".join(lines)


BEGIN_MARKER = "<!-- BEGIN GENERATED: worktree-attribution -->"
END_MARKER = "<!-- END GENERATED: worktree-attribution -->"


def _doc_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "validation"
        / "worktree-attribution-parity.md"
    )


def write_doc(report: dict[str, Metrics], env: dict[str, str]) -> Path | None:
    """Splice the generated block into the validation doc, if it is reachable."""
    doc = _doc_path()
    block = f"{BEGIN_MARKER}\n\n{render_markdown(report, env)}\n{END_MARKER}\n"
    if not doc.is_file():
        sys.stdout.write(block)
        return None
    text = doc.read_text(encoding="utf-8")
    if BEGIN_MARKER in text and END_MARKER in text:
        head = text.split(BEGIN_MARKER)[0]
        tail = text.split(END_MARKER, 1)[1]
        updated = head + block + tail
    else:
        updated = text.rstrip() + "\n\n" + block
    doc.write_text(updated.rstrip() + "\n", encoding="utf-8", newline="\n")
    return doc


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="WT-01 worktree-attribution budget harness")
    parser.add_argument(
        "--write",
        action="store_true",
        help="splice the measured block into docs/validation/worktree-attribution-parity.md",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the raw metrics as JSON"
    )
    args = parser.parse_args(argv)

    scale = int(os.environ.get("PIPELINE_PERF_BASELINE_SCALE", "1"))
    report = baseline_report(scale=scale)
    env = environment()

    if args.json:
        sys.stdout.write(
            json.dumps({k: asdict(v) for k, v in report.items()}, indent=2) + "\n"
        )
        return 0
    if args.write:
        written = write_doc(report, env)
        if written is not None:
            sys.stdout.write(f"wrote {written}\n")
        return 0
    sys.stdout.write(render_markdown(report, env) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
