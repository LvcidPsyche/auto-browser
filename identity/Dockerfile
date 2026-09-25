FROM python:3.12-slim
WORKDIR /app
COPY identity/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY approval_broker /app/approval_broker
COPY identity /app/identity
RUN useradd --system --uid 10001 identity && mkdir -p /data && chown identity:identity /data
USER identity
EXPOSE 18003
CMD ["uvicorn", "identity.app:app", "--host", "127.0.0.1", "--port", "18003", "--no-access-log"]
