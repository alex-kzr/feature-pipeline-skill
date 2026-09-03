"""BL-04 — repeatable worktree-attribution performance baseline (Phase 0).

Plan: ``docs/plans/2026-09-03-feature-pipeline-refactor.md`` (Phase 0). Prompt finding
**F-16** (``.prompts/feature-pipeline-refactor.md`` §4.4): before and after every executor
launch, :func:`pipeline_core.worktree.capture_snapshot` enumerates every tracked and
non-ignored untracked path across every repository boundary and reads each file *fully*
into memory — binaries and initialized submodules included. Snapshot I/O and peak memory
are therefore ``O(total repository bytes)`` per window, not ``O(changed files)``.

This module is the **evidence generator** for that cost. It builds two deterministic,
bounded synthetic repositories — a ``small`` one and a synthetic ``large`` one — each
carrying the full candidate matrix (tracked-clean, tracked-dirty, untracked, binary, and a
submodule-like nested boundary), then measures one executor window
(capture *before* → executor edits → capture *after* → :func:`attribute_window` →
:func:`persist_attribution`):

* ``snapshot_seconds`` — best of :data:`REPEATS` for :func:`capture_snapshot` alone, the
  hot path F-16 is about;
* ``snapshot_peak_heap_bytes`` — :mod:`tracemalloc` peak around one ``capture_snapshot``;
* ``window_seconds`` — one sample of the whole before+after+attribute+persist sequence;
* ``evidence_bytes`` — persisted implementation manifest + diff size on disk;
* the scale facts a later ``O(changed candidates)`` implementation must beat —
  ``boundary_count``, ``vcs_file_count``, ``total_repo_bytes``, and ``snapshot_bytes_read``.

:mod:`tests.test_worktree_performance_baseline` asserts the *structure* of this report —
both scenarios present, every metric populated, the recipe reproducible, and the
``O(total bytes)`` relationship F-16 predicts. It never asserts a wall-clock number; those
are recorded, with the comparison method and the environment, in
``docs/validation/worktree-performance-baseline.md`` and re-measured by WT-01.

Regenerate the doc's data block after a reviewed change::

    python -m tests.worktree_perf --write

The scenario size scales with the module constants below and with
``PIPELINE_PERF_BASELINE_SCALE`` (integer multiplier on the large scenario, default 1).

Standard library only. This module defines no ``unittest`` test case; it is a pure
harness the suite imports from.
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
    STATE_KNOWN,
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
        "attribution_state stays 'known' and changed_candidate_count is identical to this "
        "baseline for both scenarios (small and large).",
        "the classification of every changed candidate is unchanged: in-window src/** "
        "edits in_allowed_scope, pre-existing dirt still excluded from the window.",
        "evidence_bytes (manifest + diff) within +/-5% of this baseline — same attributed "
        "content, only the snapshot mechanism changes.",
        "the full tests/test_worktree.py matrix and the core unit suite stay green.",
    ],
    "improvement": [
        "large-scenario snapshot_bytes_read drops to O(changed candidates): "
        "<= 4 * LARGE_TEXT_BYTES * changed_candidate_count + 262144, i.e. at least 20x "
        "below the total_repo_bytes it reads today.",
        "large-scenario snapshot_peak_heap_bytes at least 10x lower than this baseline.",
        "large-scenario snapshot_seconds strictly below this baseline's large-scenario "
        "window_seconds on the same machine (ratio comparison only).",
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
    """One scenario's recorded baseline. Wall-clock fields are advisory."""

    scenario: str
    boundary_count: int
    vcs_file_count: int
    total_repo_bytes: int
    snapshot_bytes_read: int
    changed_candidate_count: int
    attribution_state: str
    snapshot_seconds: float
    window_seconds: float
    snapshot_peak_heap_bytes: int
    evidence_bytes: int
    matrix_tracked_clean: int
    matrix_tracked_dirty: int
    matrix_untracked: int
    matrix_binary: int
    matrix_submodule_like: int
    preexisting_dirt_excluded: bool
    in_window_edit_attributed: bool


def _on_disk_bytes(root: Path, relatives: list[str]) -> int:
    total = 0
    for relative in relatives:
        path = root / relative
        if path.is_file():
            total += path.stat().st_size
    return total


def measure(scenario: Scenario) -> Metrics:
    """Run and record one executor window over ``scenario``."""
    root, run_dir = scenario.root, scenario.run_dir

    # Snapshot hot path — best of REPEATS, no mutation between samples.
    best = float("inf")
    reference = capture_snapshot(root, exclude_roots=(run_dir,))
    for _ in range(REPEATS):
        start = time.perf_counter()
        snapshot = capture_snapshot(root, exclude_roots=(run_dir,))
        best = min(best, time.perf_counter() - start)
    assert snapshot.available, snapshot.reason

    tracemalloc.start()
    tracemalloc.clear_traces()
    capture_snapshot(root, exclude_roots=(run_dir,))
    _, peak_heap = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    vcs_files = sorted(reference.files)
    snapshot_bytes_read = sum(
        len(entry.data or b"") for entry in reference.files.values()
    )
    # Sampled at the same pre-window state the snapshot above read.
    total_repo_bytes = _on_disk_bytes(root, vcs_files)

    # One full window: before → executor edits → after → attribute → persist.
    window_start = time.perf_counter()
    before = capture_snapshot(root, exclude_roots=(run_dir,))
    _apply_window_edits(scenario)
    after = capture_snapshot(root, exclude_roots=(run_dir,))
    attribution = attribute_window(before, after, allowed_scope=ALLOWED_SCOPE)

    artifacts = launch_artifacts(run_dir, scenario.name.upper(), 1)
    artifacts.directory.mkdir(parents=True, exist_ok=True)
    artifacts.executor_report.write_text("# baseline window\n", encoding="utf-8")
    persist_attribution(
        artifacts,
        attribution,
        task_id=scenario.name.upper(),
        attempt=1,
        generation=1,
        executor_report=artifacts.executor_report,
        repo_root=root,
    )
    window_seconds = time.perf_counter() - window_start
    evidence_bytes = (
        Path(artifacts.implementation_manifest).stat().st_size
        + Path(artifacts.implementation_diff).stat().st_size
    )

    changed = {row.path: row for row in attribution.changed_files}
    preexisting_rel = scenario.preexisting_dirty.relative_to(root).as_posix()
    edit_rel = scenario.in_scope_edit.relative_to(root).as_posix()
    # dirty_b was dirtied before the window and never touched again — it must not appear.
    dirty_b_rel = "src/dirty_b.py"

    tracked_clean = sum(
        1
        for name in vcs_files
        if name.startswith("src/mod_") and name.endswith(".py")
    )

    return Metrics(
        scenario=scenario.name,
        boundary_count=1 + 1,  # root + the declared nested repository
        vcs_file_count=len(vcs_files),
        total_repo_bytes=total_repo_bytes,
        snapshot_bytes_read=snapshot_bytes_read,
        changed_candidate_count=len(attribution.changed_files),
        attribution_state=attribution.state,
        snapshot_seconds=best,
        window_seconds=window_seconds,
        snapshot_peak_heap_bytes=peak_heap,
        evidence_bytes=evidence_bytes,
        matrix_tracked_clean=tracked_clean,
        matrix_tracked_dirty=2,
        matrix_untracked=1 if "scratch/notes.txt" in vcs_files else 0,
        matrix_binary=1 if "assets/blob.bin" in vcs_files else 0,
        matrix_submodule_like=1 if "vendor/lib/inner.py" in vcs_files else 0,
        preexisting_dirt_excluded=dirty_b_rel not in changed,
        in_window_edit_attributed=(
            edit_rel in changed and preexisting_rel in changed
        ),
    )


def baseline_report(scale: int = 1) -> dict[str, Metrics]:
    """Build and measure both scenarios. ``scale`` multiplies the large file count."""
    workdir = Path(tempfile.mkdtemp(prefix="bl04-worktree-perf-"))
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
    """The machine facts WT-01 needs to compare its numbers against this baseline."""
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
            "Wall-clock numbers are machine-sensitive. WT-01 compares snapshot_seconds and "
            "window_seconds only as same-machine ratios re-measured from this harness, "
            "never against the absolute values recorded here."
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

    lines.append("## Measured baseline\n")
    lines.append(
        "Regenerated by `python -m tests.worktree_perf --write`. Wall-clock columns are "
        "**advisory** — see the Environment note.\n"
    )
    header = [
        "scenario",
        "boundary_count",
        "vcs_file_count",
        "total_repo_bytes",
        "snapshot_bytes_read",
        "changed_candidate_count",
        "attribution_state",
        "snapshot_seconds",
        "window_seconds",
        "snapshot_peak_heap_bytes",
        "evidence_bytes",
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
                    str(m.boundary_count),
                    str(m.vcs_file_count),
                    str(m.total_repo_bytes),
                    str(m.snapshot_bytes_read),
                    str(m.changed_candidate_count),
                    m.attribution_state,
                    f"{m.snapshot_seconds:.4f}",
                    f"{m.window_seconds:.4f}",
                    str(m.snapshot_peak_heap_bytes),
                    str(m.evidence_bytes),
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append(
        "F-16 is visible here: `snapshot_bytes_read` tracks `total_repo_bytes` (the "
        "snapshot reads essentially the whole repository) and is many times larger than "
        "the attributed `changed_candidate_count` needs.\n"
    )

    lines.append("## Approved budgets\n")
    lines.append(
        "The recorded `large` row is the standing budget: WT-01 must not read more bytes, "
        "hold more peak heap, or take longer (same-machine ratio) than it, and must "
        "produce identical attribution. Concrete thresholds follow.\n"
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
    for field in ("platform", "python", "implementation", "git", "cpu_count"):
        lines.append(f"| {field} | {env[field]} |")
    lines.append("")
    lines.append(env["note"] + "\n")
    return "\n".join(lines)


BEGIN_MARKER = "<!-- BEGIN GENERATED: worktree-perf -->"
END_MARKER = "<!-- END GENERATED: worktree-perf -->"


def _doc_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "validation"
        / "worktree-performance-baseline.md"
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
    parser = argparse.ArgumentParser(description="worktree-attribution performance baseline")
    parser.add_argument(
        "--write",
        action="store_true",
        help="splice the measured block into docs/validation/worktree-performance-baseline.md",
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
