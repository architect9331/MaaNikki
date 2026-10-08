# Repository Guidelines

## Project Structure & Module Organization

MaaNikki is a Windows x64 Infinity Nikki automation application using MaaFramework and an MXU-derived desktop client.

- `agent/`: Python actions, recognition, daily-task logic, and navigation; `main.py` registers handlers.
- `client/src/`: React/TypeScript components, services, stores, and themes.
- `client/src-tauri/`: Rust backend, Tauri capabilities, and application configuration.
- `interface.json`, `tasks/`, `options/`: task definitions and user-facing options.
- `resource/`: pipelines, routes, recognition images, maps, and OCR models.
- `scripts/`, `build/`, `.github/workflows/`: build tooling, dependency manifests, and Windows CI. Generated outputs belong in `.build/` or `artifacts/`.

## Build, Test, and Development Commands

See `BUILDING.md` for prerequisites. Use pinned Node/Rust versions and Python 3.11+ on the build host.

- `python scripts/release.py check`: validate source, dependency manifests, client versions, and OCR hashes.
- `powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -Action Runtime`: prepare and verify a clean runtime.
- `powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -Action Package`: compile and generate runtime/source archives and checksums.
- From `client/`, run `npx --yes pnpm@10.28.0 install --frozen-lockfile`, then `npx --yes pnpm@10.28.0 dev:app` for desktop development. Local `python/` and `maafw/` runtimes are required.
- From `client/`, `npx --yes pnpm@10.28.0 build` runs TypeScript checking and the production frontend build.

## Coding Style & Naming Conventions

Use two-space indentation in TypeScript/TSX and JSON, four spaces in Python and Rust. Match surrounding formatting. Use PascalCase for React components, camelCase for TypeScript functions, and snake_case for Python/Rust functions and route filenames.

Route UI clicks through `Runtime.action` or pipeline `nikki.ui_click`, and register actions with guarded `agent_action`. For page entrances, configure `custom_action_param.ready` and `source`. Verify local features; retry only safe entrances. Use `finish(entry)` for handoffs based on submitted order; unknown pages use normal recovery.

Rust formatting uses `client/src-tauri/rustfmt.toml` (100-column width); check with `cargo fmt --manifest-path client/src-tauri/Cargo.toml -- --check`. No dedicated JavaScript/Python formatter or lint command is configured.

## Testing Guidelines

Run `python/python.exe -B scripts/test_clicks.py` for offline click/navigation regressions. No coverage threshold is configured. Run source checks for every change; run frontend builds for UI changes and runtime verification for agent/pipeline changes. These checks do not exercise gameplay: record manual game checks separately. Name new Python tests `test_*.py`, and document their runner.

## Commit & Pull Request Guidelines

There is no commit history yet. Use scoped, imperative messages such as `fix(agent): correct route recovery`. Keep commits focused.

PRs should explain changed behavior, link related issues, list validation commands/results, and include sanitized screenshots for UI changes. Disclose untested game behavior.

## Configuration & Release Hygiene

Keep personal configuration, caches, logs, credentials, runtimes, and binaries out of Git. Synchronize Python requirements and runtime manifests; update model hashes when changing OCR assets. Preserve upstream notices in `licenses/` and `THIRD_PARTY_NOTICES.md`.
