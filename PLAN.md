# padeploy v0.1 — plan

## Context
ixl_search (`admin_tools/pa_api.py`, `deploy.py`, `traffic.py`) and toonarmycaptain_website (`deploy.py`)
each carry their own copy of PythonAnywhere deploy code, and the copies have drifted apart:
- **ixl_search:** a hardcoded file list plus `--with-data`, no 429 handling, and the traffic report.
- **Website:** git-aware selection, 429 backoff and a `uv sync` warning.
- **Both:** a failed reload still exits 0, and no request has a timeout.
- The website's `--uncommitted-changes` actually means *staged*.

The goal is one PyPI package, `padeploy` (the name is free), that both sites install as a dev dependency.
Origin: ixl_search FEEDBACK_PLAN follow-up and ixl_search TODO line 11.

Note: the website TODO says "no longer on PythonAnywhere", and PLAN_MIGRATION targets an Oracle VM or PA.
Until it moves, the website stays a consumer. ixl_search is the primary one.

## Release flow (branches `dev` → `publish`; details in RELEASING.md)
- **`ci.yml`:** runs on pushes to every branch and on all PRs. On PRs into `publish`, the `version-bump`
  job fails unless:
  - pyproject `version` is greater than the latest on PyPI, and
  - `CHANGELOG.md` has a `## [<version>]` section.
- **`publish.yml`:** runs on pushes to `publish`. Jobs: `guard` (version not on PyPI, tag doesn't
  exist) → `build` → `testpypi` → `pypi` → `tag` (`v<version>`, only after PyPI succeeds).
- **Releasing:** on `dev`, `uv version --bump …` and rename `[Unreleased]` in CHANGELOG.md → PR `dev` →
  `publish` → merge. Pushing a tag by hand doesn't publish.
- **Manual GitHub setup:** protect `publish`, and limit the `pypi`/`testpypi` environments to deploying
  from `publish`.
- A `master` staging branch (`dev` → `master` → `publish`) can be added later if needed.

## Decisions
- Scope: deploy, reload and traffic. Keep-alive and the contact module stay out.
- Don't wrap `pythonanywhere-core`. Use our own thin `requests` client instead.
- Config lives in `[tool.padeploy]` in each site's pyproject.toml.
- Publish to PyPI via trusted publishing.
- CLI: click. `cli.py` stays a thin layer over plain functions.
- **Selection:**
  - Candidates are git-tracked files, narrowed by `--changes`, `--staged` or `--since SHA`.
  - `include` is an optional list of paths or prefixes that files must match. Listing exact files reproduces
    a hardcoded list.
  - `exclude` lists paths or prefixes to leave out.
- **Groups** (paths, directory prefixes or globs):
  - Opt-in groups are left out by default and added with `--with NAME`. They may be untracked, like skills.db.
  - `--only NAME` restricts the deploy to one group, and `--code` is shorthand for `--only code`.
  - Built-in Flask defaults (all overridable): `code` = `*.py` plus any `templates/` dir, `assets` = `static/`,
    and default excludes `tests/`, `.github/`, `*.md`, `justfile`, `.pre-commit-config.yaml`.
  - pyproject.toml and uv.lock still upload, because the server needs them for `uv sync`.
- **Skip unchanged** files with a manifest at `{remote_dir}/.padeploy-manifest.json`, mapping each path to
  its sha256 plus the deployed SHA.
  - Only files whose hash differs are uploaded. `--all-files` bypasses the manifest.
  - The client updates the manifest after uploading, recording only the files that succeeded.
- **Server snapshot.** padeploy uploads `_padeploy_snapshot.py`, a stdlib-only script, with every deploy.
  - The site's WSGI file adds `import _padeploy_snapshot; _padeploy_snapshot.run("<remote_dir>")`.
  - On each app start, it hashes the real tree in a daemon thread and rewrites the manifest with
    `"source": "server"`.
  - If the hook isn't installed, the client-written manifest still works.
  - `padeploy deploy` warns once if the server manifest is missing after a reload.
- **Rate limiting:** fast by default, with 429 backoff (honor Retry-After, else 1, 2, 4, 8, 16s).
  Opt-in `--pace` keeps calls under 40/min (about 1.6s each).
  Parked: upload one archive and extract it on the server (needs a PA console via the API, unverified).

## Package layout (uv, src layout, `uv_build`, requires-python >=3.13, PythonAnywhere's newest)
```
src/padeploy/
  __init__.py            __version__
  cli.py                 click group: deploy, reload, traffic; [project.scripts] padeploy = "padeploy.cli:main"
  config.py              find pyproject.toml upward from cwd, read [tool.padeploy] → frozen dataclass,
                         merge Flask defaults; token lookup
  api.py                 PAClient: auth + User-Agent, timeouts, 429 backoff, optional pacing; upload(),
                         reload() (CNAME warning = success + warn), get_file(), list_dir()
  selection.py           git candidates → include/exclude → groups (pure, testable)
  manifest.py            hash local files, load/diff/write manifest
  deploy.py              select → diff manifest → upload (click progress bar) → manifest →
                         uv sync warning → reload (non-zero exit on failure)
  traffic.py             ported from ixl_search; domain from config; one list_dir('/var/log/') call
                         replaces probing .N/.N.gz
  _padeploy_snapshot.py  stdlib-only server hook, shipped as package data and uploaded each deploy
tests/   test_config, test_selection (tmp git repo), test_manifest, test_api (FakeSession),
         test_snapshot, test_traffic, test_cli (CliRunner): parametrized
.github/workflows/ci.yml       ruff check + ruff format --check + pytest, Python 3.13–3.14
.github/workflows/publish.yml  on push to publish: guard → uv build → TestPyPI → PyPI (OIDC) → tag
```
Runtime dependencies: `requests`, `click`. Dev dependencies: `pytest`, `ruff`.

## Config schema
```toml
[tool.padeploy]
user = "ixlsearch"                          # required
remote_dir = "/home/ixlsearch/ixlsearch"    # required
domain = "ixlsearch.pythonanywhere.com"     # default "{user}.pythonanywhere.com"
host = "www.pythonanywhere.com"             # eu.pythonanywhere.com for EU accounts
include = ["app.py", "db.py", "templates/", "static/"]   # optional
exclude = ["admin_tools/"]                  # added to the Flask default excludes

[tool.padeploy.groups]
data = { paths = ["skills.db"], default = false }    # opt-in: --with data
assets = { paths = ["static/img/"] }                 # override the built-in group
```
Token lookup order:
1. The `PA_API_TOKEN` environment variable.
2. A `PA_API_TOKEN=` line in `.secrets` next to the pyproject.
3. A hidden prompt (deploy only, and only on a TTY).

`MY_IP=` stays in `.secrets` for traffic.

## Commands
- `padeploy deploy`. Flags:
  - selection: `--changes | --staged | --since SHA`, `--with NAME…`, `--only NAME`, `--code`
  - behavior: `--all-files`, `--pace`, `--dry-run`, `--no-reload`
- `padeploy reload`
- `padeploy traffic [--my-ip] [--all | --days N] [--detail] [--top N]` (output unchanged)

Files deleted locally are reported as SKIP. Deleting them on the server is out of scope.

## Steps (one small commit each)
1. Scaffold: `uv init --package`, ruff and pytest config, .gitignore, README, MIT LICENSE,
   CHANGELOG.md, RELEASING.md.
2. config.py and its tests.
3. api.py and its tests (retries, Retry-After, pacing, CNAME warning, headers, timeouts).
4. selection.py and its tests (modes × include/exclude/groups against a tmp git repo).
5. manifest.py, `_padeploy_snapshot.py` and their tests.
6. deploy.py and the cli `deploy`/`reload` commands, with tests.
7. Port traffic.py and its tests.
8. Release flow (above). Register the trusted publishers on TestPyPI and PyPI and set up the branch
   protection and environments (manual), then release 0.0.1.
9. Migrate ixl_search:
   - add `[tool.padeploy]` and `uv add --dev padeploy`
   - update the justfile and add the WSGI hook line to DEPLOY.md
   - delete `admin_tools/` and tick the TODO
10. Migrate the website: add `[tool.padeploy]` (SKIP_PREFIXES become `exclude`), add the WSGI hook, delete deploy.py.

Later: `status` (webapp info and expiry), IPv6 /64 "my traffic" matching, remote pruning,
`--since-last-deploy` (from the manifest SHA), error-log tail.

## Verification
- In padeploy: `uv run ruff check` and `uv run pytest`. CI must be green on 3.13–3.14.
- **ixl_search:**
  - `padeploy deploy --dry-run` should list the same files as the old FILES_TO_UPLOAD.
  - A real deploy should work, and the site should load.
  - After the reload, `.padeploy-manifest.json` should show `"source": "server"`.
  - Redeploying with no changes should upload nothing.
  - `just show-traffic` should match the old output.
- **Website:** `--dry-run --changes` and `--dry-run --code` should match the old deploy.py.
- **Before the PyPI release:** `uvx --index-url https://test.pypi.org/simple/ padeploy --help`.
