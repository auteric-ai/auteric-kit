FROM rust:1-slim AS ucp-schema-builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*
RUN cargo install ucp-schema --locked

FROM python:3.12-slim
WORKDIR /app
COPY --from=ucp-schema-builder /usr/local/cargo/bin/ucp-schema /usr/local/bin/ucp-schema
COPY pyproject.toml ./
RUN pip install --no-cache-dir fastapi uvicorn httpx pydantic beautifulsoup4
COPY . .
RUN mkdir -p /data
ENV SCANNER_DB=/data/scanner.db
ENV UCP_CURRENT_VERSION=2026-08-25
EXPOSE 3100
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3100"]
