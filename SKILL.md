---
name: feature-pipeline
description: Portable core for feature-pipeline orchestration.
---

# Feature Pipeline

This repository is the independently versioned home of the portable feature-pipeline core.

## Anchors

Launchers are called with explicit anchors:

- `--project-root <path>` identifies the repository receiving feature work.
- `--agents-root <path>` identifies the shared agent configuration.
- `--core-root <path>` identifies this repository.

The core uses these anchors rather than assuming a checkout layout or host-specific paths.
