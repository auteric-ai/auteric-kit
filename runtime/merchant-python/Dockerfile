FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/auteric

RUN groupadd --system auteric && useradd --system --gid auteric --home-dir /nonexistent auteric
COPY . /opt/auteric
RUN pip install --no-cache-dir '.[sidecar,postgres]'

USER auteric
EXPOSE 8080
ENTRYPOINT ["auteric-sidecar"]
