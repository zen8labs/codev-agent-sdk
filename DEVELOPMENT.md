# Development Guide

This repository is the **zen8labs fork** of the OpenHands agent SDK. The source
lives at `github.com/zen8labs/codev-agent-sdk` and the agent-server image is
published to `ghcr.io/oadtq/agent-server` (a personal account, because the
zen8labs org blocks public packages). Everything below assumes that
namespace.

## Setup

```bash
git clone https://github.com/zen8labs/codev-agent-sdk.git
cd codev-agent-sdk
make build
```

`make build` runs `uv sync --dev` and installs pre-commit hooks. Requires
`uv >= 0.8.13` and Python 3.12–3.13.

## Code Quality

```bash
make format                              # Format code
make lint                                # Lint code
uv run pre-commit run --all-files        # Run all checks
```

Pre-commit hooks run automatically on commit. They are Python-only
(`ruff format`, `ruff check`, `pycodestyle`, `pyright`); markdown and YAML
files are not affected.

## Testing

```bash
uv run pytest                            # All tests
uv run pytest tests/sdk/                 # SDK tests only
uv run pytest tests/tools/               # Tools tests only
uv run pytest -m stress                  # Stress / scale tests
```

## Building and Publishing the agent-server Image

The agent-server is what the OpenHands app spawns per conversation. The image
is built from `openhands-agent-server/openhands/agent_server/docker/Dockerfile`
and the build is driven by `openhands-agent-server/openhands/agent_server/docker/build.py`.

### Prerequisites

- Docker with buildx (`docker buildx version`)
- A GitHub PAT with **`write:packages`** (classic) or fine-grained with
  `Packages: Write` against the namespace you publish to
- For multi-arch builds: QEMU registered
  (`docker run --privileged --rm tonistiigi/binfmt --install all`)

### One-time auth

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u oadtq --password-stdin
```

This stores the credential in `~/.docker/config.json`. Substitute the
username if you publish to a different namespace.

### Choosing the destination

The destination image is read from the `IMAGE` env var (falls back to
`--image` CLI flag, falls back to the build script's default of
`ghcr.io/oadtq/agent-server`). Override at run time — no commit needed:

```bash
IMAGE=ghcr.io/oadtq/agent-server uv run --frozen \
  ./openhands-agent-server/openhands/agent_server/docker/build.py \
  --custom-tags python --target binary --platforms linux/amd64 --push
```

For each `IMAGE`, re-run `docker login ghcr.io -u <owner> --password-stdin`
with a PAT that has `write:packages` against that owner.

### Local build and push

```bash
# Pick a buildx builder that supports multi-arch (Docker Desktop exposes one).
docker buildx use desktop-linux

# Build the binary-mode image for linux/amd64 and push to GHCR.
IMAGE=ghcr.io/oadtq/agent-server uv run --frozen \
  ./openhands-agent-server/openhands/agent_server/docker/build.py \
  --custom-tags python \
  --target binary \
  --platforms linux/amd64 \
  --push
```

The script will:

1. Resolve the workspace root and read the current git SHA.
2. `uv build --sdist` the four packages into a clean Docker build context.
3. Run the multi-stage `Dockerfile` (base builder → pyinstaller binary).
4. Tag the resulting image and push to GHCR with the standard tag set:
   `<short-sha>-python`, `<full-sha>-python`, `main-python` (branch),
   plus a base-image-slugged cache tag.

For a versioned build (when cutting a release), add `--versioned-tag` and
build from a git tag — the script will additionally emit
`<version>-python`, `<major>.<minor>-python`, `<major>-python` aliases.

For a multi-arch release, add `linux/arm64` to `--platforms`. The
`merge-manifests` step in `.github/workflows/server.yml` is not run locally;
if you need a single multi-arch manifest, run the GitHub Actions workflow
against the `main` branch on a `v*` tag push.

### Cutting a fork release

The image-tag version is the openhands-sdk package version, which must be
[PEP 440](https://peps.python.org/pep-0440/)-compliant. To mark a fork
build while keeping `pyproject.toml` PEP 440 clean, use the `+local`
segment — the standard Python way to tag a build with extra metadata:

```bash
# 1. Bump all four package versions
make set-package-version version=1.28.0+z8l.1

# 2. Build and push (the + survives into the image tag)
IMAGE=ghcr.io/oadtq/agent-server uv run --frozen \
  ./openhands-agent-server/openhands/agent_server/docker/build.py \
  --custom-tags python --target binary --platforms linux/amd64 \
  --versioned-tag --push
```

The pushed tag set will be:

```
ghcr.io/oadtq/agent-server:1.28.0+z8l.1-python
ghcr.io/oadtq/agent-server:<short-sha>-1.28.0+z8l.1-python
ghcr.io/oadtq/agent-server:<long-sha>-1.28.0+z8l.1-python
ghcr.io/oadtq/agent-server:main-1.28.0+z8l.1-python
```

Because `1.28.0+z8l.1` is not strict semver (`X.Y.Z`), the build script
does **not** emit the `1.28.0` / `1.28` / `1` alias ladder — only the
single full tag. If you need those aliases, cut a clean PEP 440 version
like `1.28.1` (no fork suffix).

For CI-driven releases, push a git tag and let the `server.yml` workflow
build with `--versioned-tag` automatically.

### Validate the pushed image

```bash
# Pull (will fail locally on Apple Silicon if the image is amd64-only)
docker pull ghcr.io/oadtq/agent-server:<short-sha>-python

# Smoke-test the running server
docker run --rm --platform linux/amd64 -p 18000:8000 \
  --name z8l-agent-smoke \
  ghcr.io/oadtq/agent-server:<short-sha>-python --port 8000 &

# Wait ~60s for the binary to start (faster on real amd64, slow on QEMU)
curl -s http://localhost:18000/alive                 # {"status":"ok"}
curl -s http://localhost:18000/server_info | jq .    # title, version, sha
```

The `build_git_sha` and `build_git_ref` fields in `/server_info` must match
the commit you built from. If you published to a different namespace
(e.g. `ghcr.io/oadtq/agent-server`), substitute that in the commands
above.

### Image visibility

GitHub Actions pushes default to **private**. Update to `public` for production use.

## CI

`.github/workflows/server.yml` builds and pushes the image on every push to
`main` and on `v*` git tags, with multi-arch manifests. The workflow
defaults `IMAGE` to `ghcr.io/oadtq/agent-server` (a personal account
because the zen8labs org blocks public packages). For the
`oadtq` namespace the workflow uses `secrets.GHCR_PUSH_TOKEN` (a PAT with
`write:packages` against `oadtq`); it falls back to `GITHUB_TOKEN` when
the image is repointed at the `zen8labs` namespace. To set the secret, go
to the repo's Settings → Secrets and variables → Actions → New repository
secret, name `GHCR_PUSH_TOKEN`, value the PAT. Triggers:

- `push` to `main` → `<IMAGE>:main-<variant>`
- `push` of a tag like `v1.28.0` → `…:1.28.0-python`, `…:1.28-python`,
  `…:1-python`, plus per-arch and per-variant tags
- `pull_request` (non-fork) → images pushed for PR validation
- `workflow_dispatch` → manual rebuild, accepts `image` and `base_image` inputs

To repoint the CI to a different namespace, set the `IMAGE` env on the
workflow's `workflow_dispatch` inputs, or change the default in
`.github/workflows/server.yml`.

## Contributing

1. Create a branch from `main`
2. Make your changes
3. `make format && make lint && uv run pre-commit run --files <changed files>`
4. Run the relevant tests (`uv run pytest tests/<domain>`)
5. Push and open a pull request
