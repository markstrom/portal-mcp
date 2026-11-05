---
description: List portal-mcp services, profiles, tools and session status
allowed-tools: mcp__plugin_portal-mcp_portal-mcp__list_services
---

Call the `list_services` tool (from the portal-mcp MCP server) and present the result to the user. Do not call any other tool.

For each service, show:
- its address (`base_url`),
- each profile and whether it has a saved session (and since when), or "not logged in",
- its tools, grouped as read/file tools and write tools (a `prepare_`/`confirm_` pair counts as one write tool),
- hidden tools and why they are hidden.

Then show load errors for broken manifests, if any, and whether a login is in progress.

If no services exist, say so and suggest `/portal-mcp:add <url>`. If the tool is not available, tell the user that the portal-mcp MCP server is not running and suggest restarting Claude Code or checking `/mcp`.
