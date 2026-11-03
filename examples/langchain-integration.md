# LangChain / LangGraph Integration

```bash
pip install auto-browser-langchain[langchain]
```

## ReAct Agent

```python
from langchain.agents import AgentExecutor, create_react_agent
from langchain_anthropic import ChatAnthropic

from auto_browser_langchain import AutoBrowserTool

tool = AutoBrowserTool(base_url="http://localhost:8000", token_file="data/api-token")
llm = ChatAnthropic(model="claude-sonnet-4-6")
agent = create_react_agent(llm, [tool])
executor = AgentExecutor(agent=agent, tools=[tool])
result = executor.invoke({"input": "Go to example.com and tell me the page title."})
print(result["output"])
```

## LangGraph Node

```python
from langgraph.graph import END, StateGraph

from auto_browser_langchain import AutoBrowserNode
from auto_browser_langchain.node import BrowserState

node = AutoBrowserNode(base_url="http://localhost:8000", token_file="data/api-token")
graph = StateGraph(BrowserState)
graph.add_node("browse", node.run)
graph.set_entry_point("browse")
graph.add_edge("browse", END)
app = graph.compile()
result = app.invoke({"goal": "Check the homepage of example.com"})
print(result)
```
