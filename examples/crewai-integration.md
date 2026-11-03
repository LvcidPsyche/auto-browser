# CrewAI Integration

```bash
pip install auto-browser-langchain[crewai]
```

```python
from crewai import Agent, Crew, Task

from auto_browser_langchain import AutoBrowserTool

browser_tool = AutoBrowserTool(base_url="http://localhost:8000", token_file="data/api-token")

researcher = Agent(
    role="Web Researcher",
    goal="Extract information from websites accurately",
    backstory="You are a precise web researcher with browser control.",
    tools=[browser_tool],
    verbose=True,
)

task = Task(
    description="Go to example.com and summarize what you find.",
    agent=researcher,
    expected_output="A summary of the example.com homepage content.",
)

crew = Crew(agents=[researcher], tasks=[task])
result = crew.kickoff()
print(result)
```
