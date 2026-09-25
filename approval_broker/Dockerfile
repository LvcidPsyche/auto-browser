FROM python:3.12-slim

WORKDIR /app
COPY approval_broker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY approval_broker /app/approval_broker

RUN useradd --create-home --uid 10001 broker
USER broker

EXPOSE 18001
CMD ["uvicorn", "approval_broker.app:app", "--host", "0.0.0.0", "--port", "18001", "--workers", "1"]
