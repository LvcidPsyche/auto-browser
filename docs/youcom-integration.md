# You.com Web Search Integration

Auto Browser now includes optional integration with You.com's search API, providing web search capabilities alongside browser automation.

## Setup

1. Get a You.com API key from [You.com API](https://api.you.com/)
2. Set the environment variable:
   ```bash
   export YDC_API_KEY=your_api_key_here
   ```
3. Restart the Auto Browser MCP server

## Available Tools

### `youcom.search`

Search the web using You.com's search API.

**Parameters:**
- `query` (required): Search query string
- `count` (optional): Number of results (1-50, default: 10)
- `offset` (optional): Starting position for results (default: 0)
- `search_type` (optional): "search" (default), "news", "images", or "videos"
- `country` (optional): Country code for localized results (default: "US")
- `safe_search` (optional): "strict", "moderate" (default), or "off"

**Example MCP call:**
```json
{
  "method": "tools/call",
  "params": {
    "name": "youcom.search",
    "arguments": {
      "query": "AI browser automation tools",
      "count": 5,
      "search_type": "search"
    }
  }
}
```

**Response:**
```json
{
  "query": "AI browser automation tools",
  "count": 5,
  "total": 1250,
  "offset": 0,
  "search_type": "search",
  "status": "success",
  "results": [
    {
      "url": "https://example.com/article",
      "title": "Top AI Browser Automation Tools",
      "description": "A comprehensive guide to AI-powered browser automation..."
    }
  ]
}
```

### `youcom.contents`

Extract full text content from a specific URL.

**Parameters:**
- `url` (required): The URL to extract content from

**Example MCP call:**
```json
{
  "method": "tools/call",
  "params": {
    "name": "youcom.contents",
    "arguments": {
      "url": "https://example.com/article"
    }
  }
}
```

**Response:**
```json
{
  "url": "https://example.com/article",
  "title": "Article Title",
  "content": "Full article text content...",
  "description": "Article description",
  "status": "success"
}
```

## Usage Patterns

### Research Workflow

1. Use `youcom.search` to find relevant articles
2. Use `browser.create_session` to start a browser session
3. Use `youcom.contents` to extract full text from promising URLs
4. Use `browser.execute_action` to navigate to and interact with interesting pages

### Content Verification

1. Find information with `youcom.search`
2. Use `youcom.contents` to get the full text
3. Use browser tools to navigate to the source and verify information visually

## Error Handling

Both tools gracefully handle missing API keys by returning error responses with helpful messages:

```json
{
  "error": "YDC_API_KEY environment variable not set",
  "status": "disabled",
  "message": "You.com search requires an API key. Set YDC_API_KEY to enable web search.",
  "results": []
}
```

This allows MCP clients to detect when the feature is unavailable and continue with other tools.

## Integration Notes

- These tools are read-only and don't affect browser state
- They work independently of browser sessions
- API calls have a 30-second timeout
- All errors are handled gracefully with informative responses
- The integration follows Auto Browser's existing patterns for external API tools