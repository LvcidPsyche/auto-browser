FROM python:3.12-slim
WORKDIR /app
COPY owner_gateway/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY owner_gateway /app/owner_gateway
RUN useradd --system --uid 10001 gateway
USER gateway
EXPOSE 18002
CMD ["uvicorn", "owner_gateway.app:app", "--host", "0.0.0.0", "--port", "18002", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
