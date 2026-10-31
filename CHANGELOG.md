# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `padeploy expiry` shows when the web app expires and how long is left, assuming it stops at 00:00 UTC on the expiry date. It exits non-zero if that's within a week, or past. `deploy` (also `--dry-run`), `reload` and `traffic` warn when it's that close.
- `FRAMEWORK` config key: `flask` (the default, unchanged behaviour) or `django`. Django leaves out every `tests/` directory and `tests.py`, and its `assets` group is every `static/` directory. `deploy` warns when a Django deploy uploads migrations or static files, which need `python manage.py migrate` or `collectstatic` run on PythonAnywhere.

## [0.3.0] - 2026-10-16

### Added
- `padeploy traffic` summarizes web app's access log/s, counting your requests separately. Your IPs come from `config --my-ip`/`MY_IP` or detected via ipify.org; networks work too, and an IPv6 address counts its whole /64. Options: `--my-ip`, `--all`, `--since DATE`, `--until DATE`, `--month [MONTH]`, `--year [YEAR]`, `--detail`, `--top N` (`false` for no limit).
- `MY_IP` config key, from the environment (`PADEPLOY_MY_IP`) or `.padeploy_secrets.toml` only.

## [0.2.1] - 2026-10-06

### Fixed
- `--changes`, `--staged` and `--since` no longer upload files removed from git with `git rm --cached` but kept on disk.
- `deploy` warns about `INCLUDE` and `--include` patterns that match no tracked file, and about untracked files they match (unless gitignored, excluded or in an opt-in group). These files were skipped silently.

## [0.2.0] - 2026-10-05

### Added
- `padeploy deploy` uploads the project's git-tracked files to `REMOTE_DIR` and reloads the web app, exiting non-zero without reloading if any upload fails. Options: `--changes`, `--staged`, `--since COMMIT`, `--with GROUP`, `--only GROUP`, `--code`, `--include`/`--exclude` (comma-separated, repeatable), `--dry-run`, `--no-reload`.
- `INCLUDE`, `EXCLUDE` and `GROUPS` now work. Opt-in groups (`default = false`) can hold untracked files; .gitignored ones must be named exactly, without wildcards.
- Patterns can be absolute paths inside the project.
- `.padeploy_secrets.toml` is excluded by default.
- `deploy` and `reload` ask for `API_TOKEN` when it isn't set and they're run in a terminal.
- `deploy` warns that `uv sync` may be needed on the server after uploading `pyproject.toml` or `uv.lock`.

### Changed
- Patterns in `INCLUDE`, `EXCLUDE` and `GROUPS` are checked when config loads: empty ones, ones containing `..`, and ones outside the project are errors.

## [0.1.0] - 2026-10-03

### Added
- Config keys `USER`/`REMOTE_DIR` (required), `DOMAIN`, `HOST`, `LOG_LEVEL`, `INCLUDE`, `EXCLUDE`, and `GROUPS`, read from the environment (as `PADEPLOY_<NAME>`), then a `.padeploy_secrets.toml` file next to pyproject.toml, then `[tool.padeploy]` in pyproject.toml. In the environment, lists and tables are TOML values, e.g. `PADEPLOY_EXCLUDE='["scripts/"]'`.
- API token as the `API_TOKEN` config key, from the environment (`PADEPLOY_API_TOKEN`) or `.padeploy_secrets.toml` only.
- Click CLI; `padeploy reload` reloads the web app, exiting non-zero on failure. PythonAnywhere's missing-CNAME response is reported as a warning. Rate-limited (429) requests are retried with backoff.
- `-v`/`--verbose` shows debug output, e.g. each rate-limit retry.

## [0.0.2] - 2026-10-02

### Fixed
- Dated the 0.0.1 changelog entry. No other changes; testing dev → publish workflow.

## [0.0.1] - 2026-10-02

### Added
- Project setup: packaging with uv, CI (ruff, pytest), PyPI release workflow.
