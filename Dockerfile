# syntax=docker/dockerfile:1

# Veritas MCP server — speaks MCP over stdio inside the container.
# An MCP client launches it with, e.g.:
#   docker run --rm -i -v "$PWD:/data" ghcr.io/advait27/veritas
# and then refers to datasets by their in-container path (e.g. /data/orders.csv).
FROM python:3.12-slim

# Bring in uv for fast, lockfile-pinned dependency installs.
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /uvx /bin/

ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    MPLBACKEND=Agg \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install runtime dependencies (no dev group) against the committed lockfile.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev

# Drop privileges; the server needs no special rights, only read access to mounted data.
RUN useradd --create-home --uid 10001 veritas && chown -R veritas:veritas /app
USER veritas

ENTRYPOINT ["veritas"]
