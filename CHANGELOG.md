# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Config keys `USER`/`REMOTE_DIR` (required), `DOMAIN`, `HOST`, `LOG_LEVEL`, `INCLUDE`, `EXCLUDE`, and `GROUPS`, read from the environment (as `PADEPLOY_<NAME>`), then a `.padeploy_secrets.toml` file next to pyproject.toml, then `[tool.padeploy]` in pyproject.toml. In the environment, lists and tables are TOML values, e.g. `PADEPLOY_EXCLUDE='["scripts/"]'`.
- API token as the `API_TOKEN` config key, from the environment (`PADEPLOY_API_TOKEN`) or `.padeploy_secrets.toml` only.

## [0.0.2] - 2026-10-02

### Fixed
- Dated the 0.0.1 changelog entry. No other changes; testing dev → publish workflow.

## [0.0.1] - 2026-10-02

### Added
- Project setup: packaging with uv, CI (ruff, pytest), PyPI release workflow.
