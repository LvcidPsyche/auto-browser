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

tool = AutoBrowserTool(base_url="http://localhost:8000", bearer_token=None)
print(tool.invoke({"action": "browser.observe", "arguments": {"preset": "text"}}))
```

`AutoBrowserNode` is a LangGraph node that opens a session and records the
current URL and screenshot in the graph state.

Full walkthroughs:
[LangChain / LangGraph](https://github.com/LvcidPsyche/auto-browser/blob/main/examples/langchain-integration.md) ·
[CrewAI](https://github.com/LvcidPsyche/auto-browser/blob/main/examples/crewai-integration.md)
