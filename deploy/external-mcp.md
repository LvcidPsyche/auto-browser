# External assistant connectivity

The running pilot has one browser and one owner. The broker at `/mcp` is a private, authenticated MCP surface; it is not a public connector URL. Each agent should get a distinct bearer credential. Credentials only authorize using the currently owner-opened browser session, not creating one. Browser site logins and the owner's TOTP seed never go to an agent.

## Current routes

- Hermes on the same host: attach through an isolated private Docker network and configure its MCP HTTP client with a named agent credential. Do not route through the public domain.
- A third-party server-side agent under owner control: use a private authenticated network path or tunnel to the broker, with a different named agent credential.
- ChatGPT: official documentation describes a Secure MCP Tunnel for private servers, but it requires an OpenAI Platform tunnel identity/key, workspace/product permissions, and connector setup. A public plugin needs a public HTTPS endpoint and suitable authentication. Account-plan support for write/browser actions must be verified in the owner's actual ChatGPT account before promising operation.
- Claude web/custom connector: Anthropic's cloud calls a public remote MCP URL. It cannot directly reach this loopback-only broker. A separate public HTTPS MCP gateway, appropriate client/user authentication, and provider-side setup are necessary; do not expose the broker's static bearer token or raw port as a shortcut.

The owner web portal and third-party MCP entry point have different authentication audiences and must not share a public route. Cloudflare Access email login is planned only for the owner portal. External assistants must never receive the owner's portal identity or TOTP code.

Sources: https://developers.openai.com/api/docs/guides/secure-mcp-tunnels ; https://developers.openai.com/plugins/concepts/plugins ; https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
