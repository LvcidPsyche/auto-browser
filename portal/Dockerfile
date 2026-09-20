FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY portal/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY approval_broker /app/approval_broker
COPY tenant_stacks /app/tenant_stacks
COPY portal /app/portal
RUN useradd --system --uid 10001 portal && mkdir -p /data && chown portal:portal /data
USER portal
EXPOSE 18004
CMD ["uvicorn", "portal.app:app", "--host", "0.0.0.0", "--port", "18004", "--no-access-log"]
