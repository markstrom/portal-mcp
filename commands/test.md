---
description: Run a service's read tools and compare the answers with their saved structure
argument-hint: <service> [profile]
allowed-tools: mcp__plugin_portal-mcp_portal-mcp__self_test, mcp__plugin_portal-mcp_portal-mcp__list_services
---

Run the portal-mcp self test. Arguments: $ARGUMENTS

The first word is the service, the optional second word is the profile. If no service was given, call `list_services` and ask the user which one.

Call the `self_test` tool with `service` (and `profile` if given). Do not set `update_baseline`.

Present the result as a short table: tool, status, and for `changed` the removed, added and changed fields. Explain the statuses:
- `ok`: the answer has the same structure as when the tool was verified.
- `baseline_saved`: there was no saved structure; it has been saved now.
- `changed`: fields were removed or changed type, so the tool may be broken.
- `error`: the call failed; show the error.
- `skipped`: not run, with the reason (write and file tools, or a read tool without `test_args`).

If any tool is `changed` or has an `error`, suggest `/portal-mcp:update <service>` to redo discovery for those tools. If the answer says a login window is open, tell the user to log in there and run the command again.
