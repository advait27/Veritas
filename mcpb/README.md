# Veritas Desktop Extension (`.mcpb`)

This directory packages Veritas as an **MCP Bundle** (`.mcpb`, formerly `.dxt`) for
one-click install into Claude Desktop. [`manifest.json`](manifest.json) is validated
against the official `@anthropic-ai/mcpb` schema (manifest version `0.2`).

This is a **thin** bundle: it does not ship Veritas's (heavy, native) Python dependencies.
Instead it launches the server with `uvx` straight from the GitHub repo, so the one
prerequisite is [`uv`](https://docs.astral.sh/uv/) on the user's `PATH`. uv fetches and
caches the server on first launch.

## Build the bundle

```sh
# from this directory
npx @anthropic-ai/mcpb pack . veritas.mcpb
```

That produces `veritas-0.1.0.mcpb`. To sanity-check it:

```sh
npx @anthropic-ai/mcpb validate manifest.json
npx @anthropic-ai/mcpb info veritas.mcpb
```

## Install

- **Locally:** open the built `.mcpb` with Claude Desktop (or drag it onto the
  Settings → Extensions pane) and confirm the install.
- **For others:** attach the built `.mcpb` to a [GitHub Release](https://github.com/advait27/Veritas/releases)
  so people can download and open it. (Optionally sign it first with
  `npx @anthropic-ai/mcpb sign`; unsigned bundles install with a warning.)

## Notes

- The bundle pins the server to the `main` branch of the repo. To pin a specific version,
  change the `args` in `manifest.json` to a tag, e.g.
  `git+https://github.com/advait27/Veritas.git@v0.1.0`.
- Prefer not to depend on `uv`? Point the `mcp_config.command` at Docker instead:
  `"command": "docker"`, `"args": ["run", "--rm", "-i", "ghcr.io/advait27/veritas"]`
  (the user then needs Docker, and must mount any data directory they want analyzed).
