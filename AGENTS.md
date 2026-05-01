# Repository Guidelines

## Project Structure & Module Organization
- Core worker, coordinator, RPC, and example plugin code live under the workspace directories such as `osprey_worker/`, `osprey_coordinator/`, `osprey_rpc/`, and `example_plugins/`.
- Documentation lives under `docs/` and `images/`.
- Workflow and integration automation lives under `.github/workflows/`.
- This repo is a fork with upstream history. Keep Divine-specific changes narrowly scoped and easy to review against upstream.

## Build, Test, and Validation Commands
- Follow `docs/DEVELOPMENT.md` for the full development workflow.
- Run the relevant Ruff, MyPy, and test commands for the code you changed.
- Use the existing integration-test workflows and local scripts when validating behavior that spans worker, coordinator, or plugin boundaries.

## Coding Style & Naming Conventions
- Follow the existing Python, Ruff, and MyPy conventions already established in the repo.
- Keep Divine-specific integration work, workflow changes, and operational adjustments scoped. Do not mix unrelated upstream-style refactors into the same PR.
- Verify whether a change is fork-specific or should stay upstream-compatible before making it.

## Security & Operational Notes
- Never commit secrets, credentials, webhook URLs, or logs containing sensitive values.
- Public issues, PRs, branch names, screenshots, and descriptions must not mention corporate partners, customers, brands, campaign names, or other sensitive external identities unless a maintainer explicitly approves it. Use generic descriptors instead.
- Be explicit about any change that affects moderation behavior, workflow publishing, or Divine-specific deployment assumptions.

## Pull Request Guardrails
- PR titles must use Conventional Commit format: `type(scope): summary` or `type: summary`.
- Set the correct PR title when opening the PR. Do not rely on fixing it later.
- If a PR title is edited after opening, verify that the semantic PR title check reruns successfully.
- Keep PRs tightly scoped. Do not include unrelated cleanup, dependency churn, or drive-by refactors.
- Temporary or transitional code must include `TODO(#issue):` with a tracking issue.
- PR descriptions must include a summary, motivation, linked issue when applicable, and manual validation plan.
- Before requesting review, note what you validated locally and what remains manual.
