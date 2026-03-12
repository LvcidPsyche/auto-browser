.PHONY: help up up-isolation up-reverse-ssh test doctor release-audit stdio-bridge smoke-isolation smoke-isolation-tunnel smoke-reverse-ssh bootstrap-codex-auth bootstrap-claude-auth bootstrap-gemini-auth bootstrap-all-auth down config config-isolation config-reverse-ssh

help: ## Show available commands
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | sort

up: ## Start the shared browser stack
	./scripts/compose_local.sh up --build

up-isolation: ## Start with per-session browser isolation enabled
	./scripts/compose_local.sh -f docker-compose.yml -f docker-compose.isolation.yml up --build

up-reverse-ssh: ## Start with the reverse-SSH sidecar profile
	./scripts/compose_local.sh --profile reverse-ssh up --build

test: ## Run controller tests in Docker
	./scripts/compose_local.sh build controller
	./scripts/compose_local.sh run --no-deps --rm controller python -m unittest discover -s tests -v

doctor: ## Run the local readiness smoke
	./scripts/doctor.sh

release-audit: ## Run the launch-prep audit
	./scripts/release_audit.sh

stdio-bridge: ## Run the stdio MCP bridge against the local HTTP MCP endpoint
	./scripts/mcp_stdio_bridge.py

smoke-isolation: ## Run the isolated-session smoke
	./scripts/smoke_isolated_session.sh

smoke-isolation-tunnel: ## Run the isolated-session tunnel smoke
	./scripts/smoke_isolated_session_tunnel.sh

smoke-reverse-ssh: ## Run the reverse-SSH smoke
	./scripts/smoke_reverse_ssh.sh

bootstrap-codex-auth: ## Sign in Codex CLI inside the controller data volume
	./scripts/bootstrap_cli_auth.sh codex

bootstrap-claude-auth: ## Sign in Claude CLI inside the controller data volume
	./scripts/bootstrap_cli_auth.sh claude

bootstrap-gemini-auth: ## Sign in Gemini CLI inside the controller data volume
	./scripts/bootstrap_cli_auth.sh gemini

bootstrap-all-auth: ## Sign in all supported CLIs inside the controller data volume
	./scripts/bootstrap_cli_auth.sh all

down: ## Stop the compose stack
	./scripts/compose_local.sh down

config: ## Render the default compose config
	./scripts/compose_local.sh config

config-isolation: ## Render the compose config with isolation override
	./scripts/compose_local.sh -f docker-compose.yml -f docker-compose.isolation.yml config

config-reverse-ssh: ## Render the compose config with reverse-SSH profile
	./scripts/compose_local.sh --profile reverse-ssh config
