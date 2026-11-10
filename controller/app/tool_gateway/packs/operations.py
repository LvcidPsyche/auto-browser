from __future__ import annotations

from ...tool_inputs import (
    CreateCronJobInput,
    CreateProxyPersonaInput,
    CronJobIdInput,
    EmptyInput,
    ProxyPersonaNameInput,
)
from ..registry import ToolSpec


def register(registry, gateway):
    for spec in [
        ToolSpec(
            name="browser.list_proxy_personas",
            description=(
                "List all configured proxy personas. "
                "Each persona assigns a named static IP/proxy to a session "
                "to prevent platform fingerprinting across agents."
            ),
            input_model=EmptyInput,
            handler=gateway._list_proxy_personas,
            pack="proxies",
        ),
        ToolSpec(
            name="browser.create_proxy_persona",
            description=(
                "Create or update a named proxy persona with server URL and credentials. "
                "Use the persona name in CreateSessionRequest.proxy_persona."
            ),
            input_model=CreateProxyPersonaInput,
            handler=gateway._create_proxy_persona,
            pack="proxies",
        ),
        ToolSpec(
            name="browser.delete_proxy_persona",
            description="Delete a named proxy persona.",
            input_model=ProxyPersonaNameInput,
            handler=gateway._delete_proxy_persona,
            pack="proxies",
        ),
        ToolSpec(
            name="browser.list_cron_jobs",
            description="List all configured cron / webhook trigger jobs.",
            input_model=EmptyInput,
            handler=gateway._list_cron_jobs,
            pack="cron",
        ),
        ToolSpec(
            name="browser.create_cron_job",
            description=(
                "Create a browser automation job that runs on a cron schedule "
                "and/or via an HTTP webhook trigger. "
                "The agent will pursue 'goal' for up to max_steps actions. "
                "Every run of the job, scheduled or triggered, starts under this call's workflow_profile "
                "(governed whatever it says where the deployment forces governed approvals)."
            ),
            input_model=CreateCronJobInput,
            handler=gateway._create_cron_job,
            governed_kind="write",
            pack="cron",
        ),
        ToolSpec(
            name="browser.delete_cron_job",
            description="Delete a cron / webhook trigger job.",
            input_model=CronJobIdInput,
            handler=gateway._delete_cron_job,
            governed_kind="destructive",
            pack="cron",
        ),
        ToolSpec(
            name="browser.trigger_cron_job",
            description="Immediately trigger a cron job (internal - no webhook auth required).",
            input_model=CronJobIdInput,
            handler=gateway._trigger_cron_job,
            governed_kind="write",
            pack="cron",
        ),
        ToolSpec(
            name="browser.rotate_cron_webhook_key",
            description=(
                "Replace a cron job's webhook key, or give the job its first. The previous key stops working at once. "
                "The new key is in this result only and cannot be read again."
            ),
            input_model=CronJobIdInput,
            handler=gateway._rotate_cron_webhook_key,
            governed_kind="write",
            # It ends the working key at once: a client that runs non-destructive tools without asking must ask.
            destructive_hint=True,
            pack="cron",
        ),
        ToolSpec(
            name="browser.revoke_cron_webhook_key",
            description=(
                "Remove a cron job's webhook key. Its webhook trigger is then refused until a new key is made; "
                "the job's schedule is not touched."
            ),
            input_model=CronJobIdInput,
            handler=gateway._revoke_cron_webhook_key,
            governed_kind="destructive",
            pack="cron",
        ),
    ]:
        registry.register(spec)
