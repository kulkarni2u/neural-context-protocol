# NCP V1 Release Checklist

## Target

- package: `neural-context-protocol`
- current version: `1.2.0`
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
- review `README.md` and `docs/NCP_V1_README_POSITIONING.md` for current promise language
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
6. Create git tag for the chosen release version.
7. Upload artifacts to PyPI.
8. Create GitHub release with benchmark and dogfood references.

## Suggested release notes outline

- what NCP is
- what this release adds
- benchmark summary
- provider parity summary
- known limitations
- what is deferred to future releases
