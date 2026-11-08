# auto-browser LangChain Integration

LangChain, LangGraph and CrewAI adapters for [Auto Browser](https://github.com/LvcidPsyche/auto-browser).
They call a running Auto Browser controller over its MCP tool endpoint.

```bash
pip install "auto-browser-langchain[langchain]"   # LangChain / LangGraph
pip install "auto-browser-langchain[crewai]"      # CrewAI
```

`AutoBrowserTool` is one LangChain tool that takes an MCP tool name and its
arguments, so the model can use every tool the controller serves:

```python
from auto_browser_langchain import AutoBrowserTool

tool = AutoBrowserTool(base_url="http://localhost:8000", token_file="data/api-token")
print(tool.invoke({"action": "browser.observe", "arguments": {"preset": "text"}}))
```

A local controller with no `API_BEARER_TOKEN` saves its generated token to
`data/api-token` in the auto-browser folder. `token_file` (on the tool, on
`AutoBrowserTool.list_tools` and on `AutoBrowserNode`) reads it on every call, so a
regenerated token needs no restart; `bearer_token` wins when it is set. A missing or
empty file raises before any request is sent.

`AutoBrowserNode` is a LangGraph node that opens a session and records the
current URL and screenshot in the graph state.

`AutoBrowserNode` needs only `httpx`. `AutoBrowserTool` needs `langchain-core`:
building it without the `[langchain]` extra raises an `ImportError` that says so.

When a tool call fails, the controller still answers HTTP 200 with `isError: true`.
`AutoBrowserTool` returns that text as `ERROR: ...` so the model can read it.
`AutoBrowserNode` raises `AutoBrowserToolError` instead, with the tool name, the error
text and the controller's structured payload (`payload`, `code`, `retryable`), so a
graph can tell a missing session from a bad selector:

```python
from auto_browser_langchain import AutoBrowserNode, AutoBrowserToolError

try:
    state = await AutoBrowserNode().run({"session_id": "s1"})
except AutoBrowserToolError as exc:
    if exc.code == "unknown_session":
        ...  # open a new session
```

An answer that is not 2xx raises httpx's `HTTPStatusError`, and a connection or
protocol failure httpx's own error; their text leaves out the token and the base
URL's userinfo, query and fragment. httpx itself logs every request at INFO on its
`httpx` logger, naming the URL as sent (userinfo, query and fragment included) and
the server's reason phrase, and this package does not filter that logger: keep a
secret in `bearer_token` or `token_file`, not in the base URL.

Full walkthroughs:
[LangChain / LangGraph](https://github.com/LvcidPsyche/auto-browser/blob/main/examples/langchain-integration.md) ·
[CrewAI](https://github.com/LvcidPsyche/auto-browser/blob/main/examples/crewai-integration.md)
