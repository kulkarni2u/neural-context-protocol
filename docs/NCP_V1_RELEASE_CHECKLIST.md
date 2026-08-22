# NCP V1 Release Checklist

## Target

- package: `neural-context-protocol`
- current version: `1.5.0`
- release posture: stable public release line (see CHANGELOG.md for full history)

## Proven already

- full repo test suite passes
  - `python3 -m pytest -p no:cacheprovider tests`
- wheel and sdist build successfully
  - `python3 -m build`
- clean install smoke passes from both artifacts
  - installed `ncp init`
  - installed `ncp status`
- repeatable local release preflight exists
  - `bash scripts/release_preflight.sh`
- launch-critical examples exist and run
- deterministic MCP dogfood loop is in place
- provider parity baseline exists for Claude, Codex, and OpenCode
- benchmark artifacts exist with real numbers

## Reminders for each publish

- confirm the intended public version in `pyproject.toml` and `ncp/version.py`
- review `README.md` and the matching `CHANGELOG.md` section for current promise language
- confirm PyPI metadata is final
  - author
  - license
  - optional dependencies
  - project URLs if desired

## Publish sequence

1. Run the full suite.
2. Build wheel and sdist.
3. Run clean-venv install smoke from both artifacts.
   - or run the combined preflight: `bash scripts/release_preflight.sh`
4. Review `CHANGELOG.md`.
5. Confirm version in:
   - `pyproject.toml`
   - `ncp/version.py`
6. Create and push `v<version>` at the verified merged `origin/main` commit.
7. Wait for `.github/workflows/release.yml` to pass; its trusted-publishing
   job builds the artifacts and uploads them to PyPI.
8. Verify the version-specific PyPI JSON and clean-install the published
   package in a fresh virtual environment.
9. Create the GitHub Release from the matching changelog section and read it
   back to verify the public metadata.

## Suggested release notes outline

- what NCP is
- what this release adds
- benchmark summary
- provider parity summary
- known limitations
- what is deferred to future releases
