# padeploy

Deploy and manage [PythonAnywhere](https://www.pythonanywhere.com) web apps from your
project repo, over the PythonAnywhere API.

## Install

```bash
uv add --dev padeploy
```

## Usage

```bash
uv run padeploy deploy
```

Uploads the project's files (see [Choosing files](#choosing-files)) to the same paths under
`REMOTE_DIR`, then reloads the web app. Files you've deleted locally stay on the server. If any
upload fails, padeploy exits non-zero without reloading.

| Option | |
|---|---|
| `--changes` | Only tracked files with uncommitted changes. |
| `--staged` | Only staged files. |
| `--since COMMIT` | Only files changed in commits since `COMMIT`. |
| `--with GROUP` | Also deploy an opt-in group. Repeatable. |
| `--only GROUP` | Only deploy this group, and any `--with` groups. |
| `--code` | Same as `--only code`. |
| `--include PATTERNS` | Comma-separated patterns, added to `INCLUDE`. Repeatable. |
| `--exclude PATTERNS` | Comma-separated patterns, added to `EXCLUDE`. Repeatable. |
| `--dry-run` | List the files without uploading them. |
| `--no-reload` | Don't reload the web app after uploading. |

```bash
uv run padeploy reload
```

Reloads the web app. Exits non-zero if the reload fails.

```bash
uv run padeploy expiry
```

Shows when the web app expires and how long is left. A free web app stops at its expiry date unless
you extend it on the PythonAnywhere Web tab. PythonAnywhere gives only the date, so padeploy takes
the web app as stopping at 00:00 UTC on it, and shows that time in UTC and in your local time. It
exits non-zero if the web app expires within a week, or already has. If PythonAnywhere gives no
expiry date, it says the web app doesn't expire.

`deploy`, `reload` and `traffic` also warn, at the end, when the web app is that close to expiring.
`deploy --dry-run` does too if `API_TOKEN` is set; it never asks for it.

```bash
uv run padeploy traffic
```

Summarizes the web app's access log: how many requests came from you and how many from everyone
else, then everyone else's top status codes, errors, paths and IPs. Your requests are the ones from
`--my-ip`, else `MY_IP`, else your public IPs as ipify.org reports them. An IPv6 address counts
its whole /64, the network your home's devices share and change their addresses within; give a
network instead, e.g. `2001:db8:1:200::/56`, to set the range yourself. By default it reads only
the current log.

| Option | |
|---|---|
| `--my-ip IPS` | Your IP addresses or networks, comma-separated. Overrides `MY_IP`. |
| `--all` | Include every rotated log. |
| `--since DATE` | Only requests from `DATE` (YYYY-MM-DD, UTC) on. |
| `--until DATE` | Only requests up to and including `DATE`. |
| `--month [MONTH]` | Only the last 30 days, or a calendar month: `2026-07`, or `July` for the most recent July. |
| `--year [YEAR]` | Only the last 365 days, or a calendar year, e.g. `2026`. |
| `--detail` | Also list everyone else's most recent requests. |
| `--top N` | Length of the top lists, or `false` for no limit. Default: 10. |

PythonAnywhere keeps a limited number of old logs, so how far back they go depends on your
traffic; a busy site's logs may cover only a couple of weeks. If a period starts before the oldest log,
the report notes how far back the logs go.

If PythonAnywhere rate-limits padeploy, it waits and retries.

## Config

padeploy can take config from three places, in order of precedence:

1. Environment variables  - prefixed with `PADEPLOY_` e.g. `PADEPLOY_USER`
2. `.padeploy_secrets.toml` at project root
3. `[tool.padeploy]` in pyproject.toml

The name is the same in all three; env vars prefix with `PADEPLOY_` to avoid potential conflicts.

Config can be split across or/and live in multiple places; you might have a `.padeploy_secrets.toml` file for local dev, and
override one or more values in CI via env vars.

| Name | |
|---|---|
| `API_TOKEN` | Required. Your PythonAnywhere API token (create one under Account → API token). Not allowed in pyproject.toml. If it isn't set, padeploy asks for it when run in a terminal. |
| `USER` | Required. Your PythonAnywhere username. |
| `REMOTE_DIR` | Required. The project's directory on PythonAnywhere, as an absolute path. `deploy` uploads files there. |
| `DOMAIN` | The web app's name on PythonAnywhere. Default: `yourname.pythonanywhere.com`. |
| `HOST` | Default: `www.pythonanywhere.com`. EU accounts need `eu.pythonanywhere.com`. |
| `FRAMEWORK` | `flask` (default) or `django`. Sets the defaults for [Choosing files](#choosing-files). See [Frameworks](#frameworks). |
| `INCLUDE` | Patterns; if set, `deploy` only uploads files matching one. See [Choosing files](#choosing-files). |
| `EXCLUDE` | Patterns `deploy` never uploads, added to the defaults. |
| `GROUPS` | Named sets of patterns, for `--only` and `--with`. |
| `MY_IP` | Your IP addresses or networks, comma-separated, for `traffic`. Not allowed in pyproject.toml. |

`DOMAIN` is the web app's name on PythonAnywhere, not necessarily the hostname visitors use. Set it
only when the web app is registered on PythonAnywhere under a custom domain.<br>
NB if using default PythonAnywhere or if a proxy (e.g. Cloudflare) forwards your domain to `yourname.pythonanywhere.com`, leave `DOMAIN` unset.

### `.padeploy_secrets.toml`

TOML; the place for `API_TOKEN`, `USER`, and `REMOTE_DIR`, which reveal your PythonAnywhere
secret/username/internal structure and so are best kept out of version control.

```toml
API_TOKEN = "your-token-here"
USER = "yourname"
REMOTE_DIR = "/home/yourname/mysite"
```

Keep your `.padeploy_secrets.toml` out of git!

### pyproject.toml

If your username is public anyway (your site is served from `yourname.pythonanywhere.com`), you can
commit config in `[tool.padeploy]` instead. Unknown names are errors, so typos fail loudly.

```toml
[tool.padeploy]
USER = "yourname"
REMOTE_DIR = "/home/yourname/mysite"
```

### Choosing files

By default, `deploy` uploads the files git tracks, except `.github/`, `.pre-commit-config.yaml`,
`.padeploy_secrets.toml` and tests (see [Frameworks](#frameworks)). To change which files it uploads:

- `INCLUDE`: only upload files matching these patterns. `deploy` warns about patterns that match
  no tracked file, and untracked files that would otherwise match (unless explicitly gitignored/excluded/opt-in).
- `EXCLUDE`: never upload files matching these patterns. These add to the defaults above, which
  can't currently be removed.
- `GROUPS`: named sets of patterns, for `--only` and `--with`. The built-in groups are `code`
  (Python files and templates) and `assets` (static files, see [Frameworks](#frameworks)); define
  one with the same name to replace it.

A group with `default = false` is opt-in, for files you don't want to upload every time, such as a
database. Its files are uploaded only with `--with NAME` or `--only NAME`, and then all of them
are, even with `--changes`, `--staged` or `--since`. They don't need to be tracked by git, but a
gitignored file or directory is only uploaded if a pattern names it exactly, without wildcards.

Patterns are relative to the project directory (where pyproject.toml is); absolute paths inside it
work too. `*` matches within a directory, `**` matches any number of directories, and a trailing
`/` matches everything in a directory: `static/` matches `static/css/site.css`, but `static` only
matches a file named `static`.

```toml
[tool.padeploy]
INCLUDE = ["app.py", "templates/", "static/"]
EXCLUDE = ["static/drafts/"]

[tool.padeploy.GROUPS]
data = { paths = ["data.db"], default = false }
```

In environment variables, these are TOML, e.g. `PADEPLOY_EXCLUDE='["scripts/"]'`. A list from the
environment or `.padeploy_secrets.toml` replaces the pyproject.toml one rather than adding to it.

### Frameworks

`FRAMEWORK` is `flask` (the default) or `django`. It sets which tests `deploy` leaves out and what
the `assets` group holds:

- `flask`: leaves out `tests/`; `assets` is `static/`.
- `django`: leaves out every `tests/` directory and `tests.py`; `assets` is every `static/`
  directory, so each app's counts.

With `django`, `deploy` also warns when it uploads migrations or static files. They need
`python manage.py migrate` or `python manage.py collectstatic` run on PythonAnywhere, then
`padeploy reload`.

```toml
[tool.padeploy]
FRAMEWORK = "django"
```

## Development

```bash
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy
```

Release workflow: see [RELEASING.md](RELEASING.md).
