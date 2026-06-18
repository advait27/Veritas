# Distributing Veritas

Veritas is a **local `stdio` server** that reads the user's local files, so it belongs in
the local-server registries and desktop-client directories — not the remote/hosted-connector
listings (those expect a hosted HTTPS + OAuth server, which doesn't fit a local
file-analysis tool).

Two install configs are referenced throughout; both avoid PyPI.

**A. `uvx` from GitHub** (needs [uv](https://docs.astral.sh/uv/)):

```json
{
  "mcpServers": {
    "veritas": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/advait27/Veritas.git", "veritas"]
    }
  }
}
```

**B. Docker** (needs Docker; mount the data you want analyzed):

```json
{
  "mcpServers": {
    "veritas": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "-v", "${HOME}:/data", "ghcr.io/advait27/veritas"]
    }
  }
}
```

## 1. Official MCP Registry

The canonical catalog; other directories increasingly sync from it. It reads
[`server.json`](../server.json) (this repo ships one referencing the GHCR OCI image).

1. Make sure the **Docker image is published and public** — push has built it to
   `ghcr.io/advait27/veritas` via the `Docker` workflow; then set the package to public in
   the repo's **Packages** settings.
2. Install the `mcp-publisher` CLI (see the `modelcontextprotocol/registry` repo).
3. Authenticate the `io.github.advait27/*` namespace (proves you own the repo):
   ```sh
   mcp-publisher login github
   mcp-publisher publish        # reads ./server.json
   ```

> The registry schema evolves. If `publish` rejects `server.json`, run `mcp-publisher init`
> to regenerate a current-schema template and copy the `packages` (OCI) block into it. The
> committed file targets the documented schema but may need a field rename (e.g.
> `registryType`/`registryBaseUrl`) to match the live validator.

## 2. Community directories

Most either crawl public GitHub repos automatically or take a short submission. A clear
README and the install config above are all they need.

| Directory | How to list |
| --- | --- |
| **Glama** (glama.ai/mcp/servers) | Auto-indexes public GitHub MCP repos; sign in to claim/curate the Veritas listing. |
| **PulseMCP** (pulsemcp.com) | Use the "Submit server" form with the repo URL. |
| **mcp.so** | Use the submit form with the repo URL + the config above. |
| **`awesome-mcp-servers`** | Open a PR adding an entry (see format below). |

Awesome-list entry (e.g. under a *Data Science / Databases* heading):

```markdown
- [Veritas](https://github.com/advait27/Veritas) 🐍 🏠 — hypothesis-driven data
  investigator; every numeric claim is verified against an executed artifact, with
  FDR-suppressed discovery. (DuckDB, local files)
```

## 3. Claude Desktop Extension

A one-click `.mcpb` bundle lives in [`../mcpb/`](../mcpb/). Pushing a version tag runs the
[Desktop Extension workflow](../.github/workflows/extension.yml), which builds the `.mcpb`
and attaches it to the matching
[GitHub Release](https://github.com/advait27/Veritas/releases) for users to download and
open in Claude Desktop. See [mcpb/README.md](../mcpb/README.md).

## Not applicable

- **Claude.ai connector directory / Smithery hosting / other remote-MCP catalogs** — these
  run your server remotely and can't reach a user's local files. Veritas is local-first by
  design, so list it where local servers belong (above).
