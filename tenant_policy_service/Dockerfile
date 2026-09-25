FROM docker:27-cli

RUN apk add --no-cache python3 py3-pip && python3 -m venv /venv
ENV PATH="/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY tenant_policy_service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY tenant_stacks /app/tenant_stacks
COPY deploy/tenants /app/deploy/tenants
COPY tenant_policy_service /app/tenant_policy_service
EXPOSE 18005
CMD ["uvicorn", "tenant_policy_service.app:app", "--host", "0.0.0.0", "--port", "18005", "--no-access-log"]
