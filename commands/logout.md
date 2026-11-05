---
description: Log out of a service and delete its local session
argument-hint: <service> [profile]
allowed-tools: mcp__plugin_portal-mcp_portal-mcp__logout, mcp__plugin_portal-mcp_portal-mcp__list_services
---

Log out of a portal-mcp service. Arguments: $ARGUMENTS

The first word is the service, the optional second word is the profile. If no service was given, call `list_services` and ask the user which one.

Call the `logout` tool with `service` (and `profile` if given). Without a profile, every profile of the service is logged out.

Tell the user which profiles were logged out, what the service's logout endpoint answered, and that the local session is deleted. The browser profile is kept, so the service still recognizes the device the next time the user logs in.
