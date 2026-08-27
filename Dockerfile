# transport-lit as a Streamable-HTTP MCP server (for Open WebUI, LibreChat, n8n, remote clients).
#   docker build -t transport-lit .
#   docker run -v transport-lit-data:/data -e TRANSPORT_LIT_CONTACT=you@example.org transport-lit transport-lit harvest --source all
#   docker run -v transport-lit-data:/data -p 8765:8765 transport-lit
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev && uv pip install --no-deps .
ENV PATH="/app/.venv/bin:$PATH" TRANSPORT_LIT_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8765
CMD ["transport-lit-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765"]
