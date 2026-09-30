FROM node:22-slim AS web
WORKDIR /web
COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/package.json
RUN npm ci
COPY apps/web apps/web
WORKDIR /web/apps/web
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/commerce/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY packages/commerce-starter/src/auteric_commerce /app/auteric_commerce
COPY packages/commerce-contracts/generated/python /app/packages/commerce-contracts/generated/python
COPY kits/auteric-kit/runtime/sdk/src/auteric_edge /app/auteric_edge
COPY services/__init__.py /app/services/__init__.py
COPY services/commerce /app/services/commerce
COPY apps/shopify/shopify.app.toml /app/apps/shopify/shopify.app.toml
COPY --from=web /web/apps/web/dist /app/apps/web/dist
COPY --from=web /web/apps/web/public /app/apps/web/public
RUN useradd --uid 10001 --create-home gateway && mkdir -p /app/.runtime/commerce && chown -R gateway:gateway /app/.runtime
USER 10001
EXPOSE 8080
CMD ["uvicorn", "services.commerce.app:factory", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
