# auto-browser-client

Python SDK for the Auto Browser REST API.

```python
from auto_browser_client import AutoBrowserClient

client = AutoBrowserClient("http://localhost:8000", token="secret")
session = client.create_session(start_url="https://example.com")
client.navigate(session["id"], "https://example.com/dashboard")
client.close_session(session["id"])
```
