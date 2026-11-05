---
description: Add a new logged-in web service as MCP tools (discovery, recording, catalog, manifest)
argument-hint: <url of the service>
---

Add a new service to portal-mcp. The service's address is: $ARGUMENTS

Use the `portal-mcp-creator` skill and follow its pipeline from step 1, including all of its rules. In short:

1. If no address was given above, ask the user for it.
2. Call the `list_services` tool to find `services_dir`. If a manifest for this host already exists, say so and offer `/portal-mcp:update` instead.
3. Ask which parts of the service the user needs, then run reconnaissance, guided recording (the user logs in), capture of writes without performing them, analysis, catalog and the user's choice.
4. Show the manifest and save it only after the user approves it.
5. Verify the tools, save the `self_test` baseline, delete the recordings and commit the manifest in the local manifest repo.

Never ask for, type or store a password. Never approve a write action on the user's behalf.
