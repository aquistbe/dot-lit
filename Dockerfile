# dot-lit as a Streamable-HTTP MCP server (for Open WebUI, LibreChat, n8n, remote clients).
#   docker build -t dot-lit .
#   docker run -v dot-lit-data:/data -e DOT_LIT_CONTACT=you@example.org dot-lit dot-lit harvest --source all
#   docker run -v dot-lit-data:/data -p 8765:8765 dot-lit
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev && uv pip install --no-deps .
ENV PATH="/app/.venv/bin:$PATH" DOT_LIT_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8765
CMD ["dot-lit-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765"]
