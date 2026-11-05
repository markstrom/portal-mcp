---
description: Add a new logged-in web service as MCP tools (discovery, recording, catalog, manifest)
argument-hint: <url of the service>
---

Add a new service to portal-mcp. The service's address is: $ARGUMENTS

Use the `portal-mcp-creator` skill and follow its pipeline from step 1, including all of its rules. In short:

1. If no address was given above, ask the user for it. Ask nothing else before the recording starts: do not ask which parts of the service the user needs.
2. Call the `list_services` tool to find `services_dir`. If a manifest for this host already exists, say so and offer `/portal-mcp:update` instead.
3. Go straight on: reconnaissance, a draft manifest, and a recording with `--discover --explore` where the user logs in, the menu pages are visited and their safe controls are clicked by the plugin. Then capture of writes without performing them (only if the user wants a form), analysis, catalog, and the user's choice. The catalog is the first place the user is asked what they want.
4. Show the manifest and save it only after the user approves it.
5. Verify the tools, save the `self_test` baseline, delete the recordings and commit the manifest in the local manifest repo.

Never ask for, type or store a password. Never approve a write action on the user's behalf.
