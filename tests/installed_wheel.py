"""PKG-03 — build a wheel, install it in isolation, and exercise it.

Plan: ``docs/plans/2026-09-03-feature-pipeline-refactor.md`` (Phase 1),
``docs/plans/tasks/PKG-03_installed-wheel-ci.md``.

This module is a **harness**, not a test case. :mod:`tests.test_installed_wheel` drives it
in the normal unit suite; ``python -m tests.installed_wheel`` regenerates the local
acceptance evidence in ``docs/validation/installed-package.md``.

What it proves (PKG-03 acceptance criteria):

* **AC-1** — ``uv build --wheel`` produces a wheel that ``uv pip install`` puts into a
  throwaway environment, and the ``feature-pipeline`` console command then starts from an
  unrelated working directory with **no source-tree path injection** (the installed
  ``pipeline_core`` / ``schemas`` / ``feature_pipeline`` resolve from ``site-packages``).
* **AC-2** — the same isolated-install contract is what CI runs on Windows and Linux; the
  supported matrix is :data:`SUPPORTED_MATRIX` and the CI job at
  :data:`CI_WORKFLOW` reads it.
* **AC-3** — ``python -m unittest discover`` passes both from the project root and against
  a copy of :mod:`tests.test_installed_contract` that can only import through the wheel.

The heavy steps are cheap here because the runtime has **no dependencies** (``docs/adr/003``):
``uv build`` reuses its cached build backend and ``uv venv`` + ``uv pip install`` copy a
single wheel. Results are cached per process so the suite builds once.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

_CORE_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _CORE_ROOT.parent


def redact(text: str) -> str:
    """Strip host-specific absolute paths from anything that reaches a committed file.

    AGENTS.md standing rule 6 / the portable-core anchor contract: no user-home absolute
    path is ever written into a report, config, or code file. Order matters — the most
    specific root first.
    """
    for needle, token in (
        (str(_CORE_ROOT), "<core_root>"),
        (str(_REPO_ROOT), "<repo_root>"),
        (str(Path.home()), "<home>"),
        (str(Path(tempfile.gettempdir())), "<tmp>"),
    ):
        if needle:
            text = text.replace(needle, token).replace(needle.replace("\\", "/"), token)
    return text

#: The console command the wheel installs (``pyproject.toml`` ``[project.scripts]``).
CONSOLE_COMMAND = "feature-pipeline"

#: The three import namespaces a correct wheel makes available without the source tree.
INSTALLED_NAMESPACES = ("pipeline_core", "schemas", "feature_pipeline")

_PUSH_DENIED_MESSAGE = "push is denied at this stage and the runner has no push code path."

#: The single portable contract module run against the isolated install (AC-3).
CONTRACT_MODULE = "test_installed_contract"

#: CI matrix — the OS images and interpreter versions that must run the isolated-install
#: contract (AC-2). ``uv`` provisions each interpreter, so the list is the *supported*
#: Python floor (``requires-python >= 3.11``, ``docs/adr/003``) through the current release.
SUPPORTED_MATRIX: dict[str, list[str]] = {
    "ubuntu-latest": ["3.11", "3.12", "3.13"],
    "windows-latest": ["3.11", "3.12", "3.13"],
}

#: The workflow that runs :data:`SUPPORTED_MATRIX`. Logical, under the repository root.
CI_WORKFLOW = ".github/workflows/installed-package.yml"

BEGIN_MARKER = "<!-- BEGIN GENERATED: installed-package -->"
END_MARKER = "<!-- END GENERATED: installed-package -->"


class HarnessError(RuntimeError):
    """A step of the isolated-install recipe could not run (missing ``uv``, build fail)."""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class AcceptanceEvidence:
    wheel_name: str
    python_version: str
    platform: str
    site_packages_root: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def uv_available() -> bool:
    return shutil.which("uv") is not None


def _run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, **(env or {})},
    )


# --- recipe steps ------------------------------------------------------------------------


def build_wheel(out_dir: Path) -> Path:
    """``uv build --wheel`` the core into *out_dir*; return the built wheel path."""
    if not uv_available():
        raise HarnessError("uv is not on PATH; the isolated-install recipe needs it")
    done = _run(["uv", "build", "--wheel", "--out-dir", str(out_dir)], cwd=_CORE_ROOT)
    if done.returncode != 0:
        raise HarnessError(f"uv build failed:\n{done.stdout}\n{done.stderr}")
    wheels = sorted(out_dir.glob("*.whl"))
    if not wheels:
        raise HarnessError(f"uv build reported success but wrote no wheel into {out_dir}")
    return wheels[-1]


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_console(venv_dir: Path) -> Path:
    scripts = "Scripts" if os.name == "nt" else "bin"
    name = f"{CONSOLE_COMMAND}.exe" if os.name == "nt" else CONSOLE_COMMAND
    return venv_dir / scripts / name


def create_isolated_install(wheel: Path, venv_dir: Path) -> Path:
    """``uv venv`` + ``uv pip install <wheel>``; return the venv interpreter path."""
    done = _run(["uv", "venv", str(venv_dir)], cwd=_REPO_ROOT)
    if done.returncode != 0:
        raise HarnessError(f"uv venv failed:\n{done.stdout}\n{done.stderr}")
    python = _venv_python(venv_dir)
    done = _run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)], cwd=_REPO_ROOT
    )
    if done.returncode != 0:
        raise HarnessError(f"uv pip install failed:\n{done.stdout}\n{done.stderr}")
    return python


def _site_packages(python: Path) -> Path:
    done = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        capture_output=True, text=True,
    )
    return Path(done.stdout.strip())


def collect_evidence() -> AcceptanceEvidence:
    """Run the whole recipe once and return a structured pass/fail record."""
    workdir = Path(tempfile.mkdtemp(prefix="pkg03-"))
    elsewhere = workdir / "unrelated-cwd"
    elsewhere.mkdir()
    try:
        wheel = build_wheel(workdir / "dist")
        python = create_isolated_install(wheel, workdir / "venv")
        site_packages = _site_packages(python)
        ev = AcceptanceEvidence(
            wheel_name=wheel.name,
            python_version=platform.python_version(),
            platform=platform.platform(),
            site_packages_root=redact(str(site_packages)),
        )

        # AC-1 — the shipped namespaces import from site-packages, not the checkout.
        probe = (
            "import json, pipeline_core, schemas, feature_pipeline; "
            "print(json.dumps({n: getattr(__import__(n), '__file__', '') "
            "for n in ['pipeline_core', 'schemas', 'feature_pipeline']}))"
        )
        done = _run([str(python), "-I", "-c", probe], cwd=elsewhere)
        located = json.loads(done.stdout or "{}") if done.returncode == 0 else {}
        from_install = done.returncode == 0 and all(
            located.get(n, "").startswith(str(site_packages)) for n in INSTALLED_NAMESPACES
        )
        ev.checks.append(Check(
            "namespaces_import_from_site_packages", from_install,
            done.stderr.strip() if not from_install else "",
        ))
        ev.checks.append(Check(
            "no_source_tree_on_path",
            done.returncode == 0 and str(_CORE_ROOT) not in done.stdout,
            "source-tree path present in resolved module files" if str(_CORE_ROOT) in done.stdout else "",
        ))

        # AC-1 — the console command starts from an unrelated directory.
        console = _venv_console(workdir / "venv")
        done = _run([str(console), "--help"], cwd=elsewhere)
        ev.checks.append(Check(
            "console_help_exit_zero",
            done.returncode == 0 and "usage" in done.stdout.lower(),
            done.stderr.strip() if done.returncode != 0 else "",
        ))
        done = _run([str(console), "--push"], cwd=elsewhere)
        ev.checks.append(Check(
            "console_push_denied",
            done.returncode == 1 and done.stderr.strip() == _PUSH_DENIED_MESSAGE,
            done.stderr.strip(),
        ))

        # AC-3 — root discovery: the installed runner still refuses to infer a layout.
        done = _run(
            [str(python), "-I", "-m", "pipeline_core.runner_cli", "--mode", "execute"],
            cwd=elsewhere,
        )
        ev.checks.append(Check(
            "root_discovery_fail_closed",
            done.returncode not in (0,) and str(_CORE_ROOT) not in done.stderr,
            "",
        ))

        # AC-3 — installed-package test discovery over a bare copy of the contract module.
        pkgtests = workdir / "pkgtests"
        pkgtests.mkdir()
        shutil.copy2(_CORE_ROOT / "tests" / f"{CONTRACT_MODULE}.py", pkgtests / f"{CONTRACT_MODULE}.py")
        done = _run(
            [str(python), "-m", "unittest", "discover", "-s", str(pkgtests),
             "-t", str(pkgtests), "-p", f"{CONTRACT_MODULE}.py", "-v"],
            cwd=elsewhere,
        )
        tail = "\n".join((done.stderr or done.stdout).splitlines()[-3:])
        ev.checks.append(Check("installed_package_discovery", done.returncode == 0, tail))

        return ev
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --- evidence document -----------------------------------------------------------------


def _doc_path() -> Path:
    return _REPO_ROOT / "docs" / "validation" / "installed-package.md"


def render_markdown(ev: AcceptanceEvidence) -> str:
    lines = ["## Supported version matrix", ""]
    lines.append("`uv` provisions each interpreter; every cell runs the isolated-install "
                 "contract from `.github/workflows/installed-package.yml`.")
    lines.append("")
    lines.append("| OS image | Python versions |")
    lines.append("|---|---|")
    for image, versions in SUPPORTED_MATRIX.items():
        lines.append(f"| `{image}` | {', '.join(versions)} |")
    lines.append("")
    lines.append("## Local acceptance evidence")
    lines.append("")
    lines.append(f"Regenerated by `python -m tests.installed_wheel --write`. "
                 f"Wheel `{ev.wheel_name}`, Python {ev.python_version}, {ev.platform}.")
    lines.append("")
    lines.append("| Check | Result |")
    lines.append("|---|---|")
    for c in ev.checks:
        mark = "pass" if c.ok else f"FAIL — {c.detail or 'see harness output'}"
        lines.append(f"| `{c.name}` | {mark} |")
    lines.append("")
    lines.append(f"Overall: **{'pass' if ev.ok else 'FAIL'}** "
                 f"({sum(c.ok for c in ev.checks)}/{len(ev.checks)} checks).")
    lines.append("")
    lines.append("## Environment")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| platform | {platform.platform()} |")
    lines.append(f"| python | {platform.python_version()} |")
    lines.append(f"| implementation | {platform.python_implementation()} |")
    lines.append(f"| uv | {_uv_version()} |")
    lines.append(f"| purelib (redacted) | {redact(sysconfig.get_path('purelib') or '')} |")
    lines.append("")
    # No host-specific absolute path survives into the committed doc (AGENTS.md rule 6).
    return redact("\n".join(lines))


def _uv_version() -> str:
    if not uv_available():
        return "not found"
    done = subprocess.run(["uv", "--version"], capture_output=True, text=True)
    return done.stdout.strip() or "unknown"


def write_doc(ev: AcceptanceEvidence) -> Path | None:
    doc = _doc_path()
    block = f"{BEGIN_MARKER}\n\n{render_markdown(ev)}\n{END_MARKER}\n"
    if not doc.is_file():
        sys.stdout.write(block)
        return None
    text = doc.read_text(encoding="utf-8")
    if BEGIN_MARKER in text and END_MARKER in text:
        updated = text.split(BEGIN_MARKER)[0] + block + text.split(END_MARKER, 1)[1]
    else:
        updated = text.rstrip() + "\n\n" + block
    doc.write_text(updated.rstrip() + "\n", encoding="utf-8", newline="\n")
    return doc


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="PKG-03 isolated-install acceptance harness")
    parser.add_argument("--write", action="store_true",
                        help="splice the evidence block into docs/validation/installed-package.md")
    parser.add_argument("--json", action="store_true", help="print the raw evidence as JSON")
    args = parser.parse_args(argv)

    ev = collect_evidence()
    if args.json:
        sys.stdout.write(redact(json.dumps(asdict(ev), indent=2, default=str)) + "\n")
        return 0 if ev.ok else 1
    if args.write:
        written = write_doc(ev)
        sys.stdout.write(f"wrote {written}\n" if written else "")
        return 0 if ev.ok else 1
    sys.stdout.write(render_markdown(ev) + "\n")
    return 0 if ev.ok else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
