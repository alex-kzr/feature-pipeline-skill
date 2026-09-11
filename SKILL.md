---
name: feature-pipeline
description: Use when developing, diagnosing, or understanding the portable feature-pipeline core. For project run operations use feature-pipeline-operator; for project onboarding use feature-pipeline-project-setup.
---

# Feature Pipeline

This repository is the independently versioned home of the portable feature-pipeline core.

## Select the right skill

- Operate a run in any project — preflight, dry run, execution, recovery, monitoring, or evidence review — with [feature-pipeline-operator](../feature-pipeline-operator/SKILL.md).
- Onboard a repository or generate its project-local configuration with [feature-pipeline-project-setup](../feature-pipeline-project-setup/SKILL.md).
- Use this skill when changing, diagnosing, or understanding the portable core itself.

## Anchors

Launchers are called with explicit anchors:

- `--project-root <path>` identifies the repository receiving feature work.
- `--agents-root <path>` identifies the shared agent configuration.
- `--core-root <path>` identifies this repository.

The core uses these anchors rather than assuming a checkout layout or host-specific paths.

## Hermes operation

For Hermes-initiated work through an external Codex or Claude worker, follow the complete
project-neutral procedure in [the Hermes operator guide](docs/hermes-operator.md).

