# Releasing

Releases go to PyPI from the `publish` branch. Work happens on `dev`. A release is a PR from `dev` into
`publish`. Merging it publishes the package and tags the commit.

## One-time setup

1. **PyPI and TestPyPI trusted publishers.** On [pypi.org](https://pypi.org/manage/account/publishing/)
   and [test.pypi.org](https://test.pypi.org/manage/account/publishing/), separate accounts with 2FA on
   each, go to *Publishing* and add a pending publisher (GitHub):
   - Project name: `padeploy`
   - Owner: `toonarmycaptain`, Repository: `padeploy`
   - Workflow: `publish.yml`
   - Environment: `pypi` on PyPI, `testpypi` on TestPyPI

   A pending publisher doesn't reserve the name. The project is created on the first upload.
2. **GitHub environments** (*Settings → Environments*): create `testpypi` and `pypi`. Under
   *Deployment branches*, allow only `publish`.

   Optional: add yourself as a required reviewer on `pypi`. Each release then pauses after the TestPyPI
   upload until you approve it.
3. **Protect the `publish` branch** (*Settings → Branches*):
   - Require a pull request before merging.
   - Require the `lint`, `test` and `version-bump` status checks to pass.
   - Block force pushes and deletions.

## Making a release

1. On `dev`, bump the version:
   ```bash
   uv version --bump patch   # bug fixes only
   uv version --bump minor   # new features; also breaking changes while on 0.x
   uv version --bump major   # breaking changes after 1.0
   ```
2. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [<version>] - <YYYY-MM-DD>` and add a fresh,
   empty `## [Unreleased]` above it.
3. Commit `pyproject.toml`, `uv.lock` and `CHANGELOG.md`.
4. Open a PR from `dev` into `publish`.
5. Merge it. The publish workflow:
   1. checks the version isn't already on PyPI and its `v<version>` tag doesn't exist
   2. runs the tests and builds the package
   3. uploads to TestPyPI, then PyPI
   4. tags the release commit `v<version>`

Pushing a tag by hand doesn't publish anything.

## What the checks catch

- **`version-bump`** (on PRs into `publish`) fails if:
  - the version isn't newer than the latest on PyPI. It checks that the version went up, not which
    part did.
  - `CHANGELOG.md` has no `## [<version>]` section.
- **`guard`** (in the publish workflow) stops before any upload if the version is already on PyPI or
  already tagged.

## If a publish fails

- **Before any upload** (guard, tests or build): fix the problem on `dev` and open a new release PR.
- **TestPyPI succeeded but PyPI failed:** use *Re-run failed jobs* on the workflow run. It reuses the
  same build, and TestPyPI isn't uploaded again.
- **PyPI succeeded but tagging failed:** tag the release commit by hand:
  `git tag v<version> <commit> && git push origin v<version>`.

A version number can be uploaded to PyPI only once, even if it's later deleted. When in doubt, bump the
version and release again.