# padeploy

Deploy and manage [PythonAnywhere](https://www.pythonanywhere.com) web apps from your
project repo, over the PythonAnywhere API.

## Settings

padeploy can take settings from three places, in order of precedence:

1. Environment variables  - prefixed with `PADEPLOY_` e.g. `PADEPLOY_USER`
2. `.padeploy_secrets`
3. `[tool.padeploy]` in pyproject.toml

The name is the same in all three; env vars prefix with `PADEPLOY_` to avoid potential conflicts.

Settings can be split across or/and live in multiple places; you might have a `.padeploy_secrets` file for local dev, and
override one or more values in CI via env vars.

Your project root is the directory of the nearest pyproject.toml at or above the current directory;
`.padeploy_secrets` goes there.

| Name | |
|---|---|
| `API_TOKEN` | Required. Your PythonAnywhere API token (create one under Account → API token). Not allowed in pyproject.toml. |
| `USER` | Required. Your PythonAnywhere username. |
| `REMOTE_DIR` | Required. The project's directory on PythonAnywhere, as an absolute path. |
| `DOMAIN` | The web app's name on PythonAnywhere. Default: `yourname.pythonanywhere.com`. |
| `HOST` | Default: `www.pythonanywhere.com`. EU accounts need `eu.pythonanywhere.com`. |

`DOMAIN` is the web app's name on PythonAnywhere, not necessarily the hostname visitors use. Set it
only when the web app is registered on PythonAnywhere under a custom domain.<br>
NB if using default PythonAnywhere or if a proxy (e.g. Cloudflare) forwards your domain to `yourname.pythonanywhere.com`, leave `DOMAIN` unset.

### `.padeploy_secrets`

One `NAME=value` per line. Lines starting with `#` are ignored; a `#` later in a line is part of the
value. It's the place for `API_TOKEN`, and for `USER` and `REMOTE_DIR`, which reveal your
PythonAnywhere username and so are best kept out of version control.

```
API_TOKEN=your-token-here
USER=yourname
REMOTE_DIR=/home/yourname/mysite
```

Keep your `.padeploy_secrets` out of git!

### pyproject.toml

If your username is public anyway (your site is served from `yourname.pythonanywhere.com`), you can
commit settings in `[tool.padeploy]` instead. Unknown names are errors, so typos fail loudly.

```toml
[tool.padeploy]
USER = "yourname"
REMOTE_DIR = "/home/yourname/mysite"
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
