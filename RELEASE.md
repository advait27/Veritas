# Releasing veritas-mcp

Releases are published to PyPI by GitHub Actions using **Trusted Publishing** (OIDC), so no
API tokens are ever stored in the repo or on a developer machine. Pushing a version tag
builds, gates, and publishes the package.

## One-time setup: register the PyPI trusted publisher

Do this once, before the first release, while logged in to PyPI:

1. Go to <https://pypi.org/manage/account/publishing/> ("Add a new pending publisher").
2. Fill in:
   - **PyPI Project Name:** `veritas-mcp`
   - **Owner:** your GitHub user or org (e.g. `OWNER`)
   - **Repository name:** `veritas-mcp`
   - **Workflow name:** `release.yml`
   - **Environment name:** `pypi`
3. Save. PyPI will now accept an OIDC-authenticated upload from this repo's `release.yml`
   workflow running in the `pypi` environment — which is exactly what
   [`.github/workflows/release.yml`](.github/workflows/release.yml) requests.

(Optional but recommended: in the GitHub repo settings, create an Environment named `pypi`
and add required reviewers, so a release upload needs a manual approval.)

## Cutting a release

1. Update the version in **two** places — they must match:
   - `version` in [`pyproject.toml`](pyproject.toml)
   - `__version__` in [`src/veritas/__init__.py`](src/veritas/__init__.py)
2. Add a dated section to [`CHANGELOG.md`](CHANGELOG.md).
3. Commit, then tag and push:

   ```sh
   git commit -am "release: vX.Y.Z"
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```

Pushing the `vX.Y.Z` tag triggers `release.yml`: it runs the full gate (format, lint,
mypy, tests), builds the sdist + wheel, checks the metadata, and publishes to PyPI. The tag
version and the package version must agree.

## Verifying

- Watch the **Release** workflow under the repo's Actions tab.
- After it succeeds, confirm the version at <https://pypi.org/project/veritas-mcp/> and
  smoke-test the published package:

  ```sh
  uvx veritas-mcp   # should start the stdio server (Ctrl-D to exit)
  ```
