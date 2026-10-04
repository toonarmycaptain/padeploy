# Releasing

Development happens on `dev`. A release is a pull request from `dev` into `publish`. Merging it runs
`publish.yml`, which uploads to TestPyPI, then to PyPI after deployment approval, and tags the commit
`v<version>`.

1. On `dev`, run `uv version --bump patch|minor|major`.
2. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [<version>] - <YYYY-MM-DD>` and add a new, empty
   `## [Unreleased]` above it.
3. Commit, push, and open a pull request into `publish`. The `version-bump` check requires the version
   to be newer than the latest on PyPI and to have a `CHANGELOG.md` entry.
4. Merge.

If the `tag` job fails after the PyPI upload, tag the commit manually:
`git tag v<version> <commit> && git push origin v<version>`.
