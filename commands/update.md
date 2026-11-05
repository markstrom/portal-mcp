---
description: Explore more parts of a service, or redo discovery for tools that stopped working
argument-hint: <service> [what to add or fix]
---

Update the portal-mcp service described here: $ARGUMENTS

Use the `portal-mcp-creator` skill and its rules.

1. If no service was given, call `list_services` and ask the user which service.
2. Call `self_test` for the service (and the profile, if the user named one).
3. Tools with status `changed` or `error`: follow the skill's section "Repairing tools that stopped working".
4. New functions the user asked for: follow "Adding functions to an existing service". Add the new tools to the same manifest; do not rename existing tools without asking.
5. Show the changed manifest and save it only after the user approves it. Then verify, update the baseline only if the user agrees, delete the recordings and commit the manifest.
