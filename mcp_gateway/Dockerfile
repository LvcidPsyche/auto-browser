FROM python:3.12.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY mcp_gateway/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY mcp_gateway /app/mcp_gateway
RUN useradd --system --uid 10001 gateway && mkdir -p /var/lib/mcp-gateway && chown gateway:gateway /var/lib/mcp-gateway
USER gateway
CMD ["uvicorn", "mcp_gateway.app:app", "--host", "0.0.0.0", "--port", "18002", "--proxy-headers", "--forwarded-allow-ips", "10.0.0.0/8"]
