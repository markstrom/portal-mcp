---
name: portal-mcp-creator
description: Create read-only MCP tools for a logged-in web service that has no open API. Use when the user wants to add a service or new read functions (listing, viewing, downloading) from a website or customer portal, or update the tools of an existing service. Covers discovery, guided recording, analysis, catalog, the user's choice, manifest generation and verification. Write tools are not part of this version.
---

# portal-mcp-creator

Creates a YAML manifest in `services/<name>.yaml` under the portal-mcp data directory. The runtime then exposes the chosen read and file tools as MCP tools.

## Rules (never break these)

1. **The user logs in, never you.** You never ask for, type, or store a password, token or one-time code. Login happens in the visible browser window that `record.py` opens.
2. **Read-only.** Only GET requests are recorded after login. Do not work around the guard: do not write your own requests to the service, and do not use a browser other than the one provided by the scripts.
3. **Stay on the service's own host.** The guard blocks other hosts. Do not follow links to third-party sites.
4. **Keep data out of the repo and out of chat.** Recordings contain cookies and response bodies. Store them only under the recordings directory, never in the plugin repo, and delete them in step 12. Quote no personal data from responses in your replies.
5. **Write descriptions yourself.** Tool descriptions are written from the analysis. Never copy text from the service's code or pages.
6. **Confirm before you write files.** Show the manifest and wait for the user's approval before saving it.
7. **Only the user chooses tools.** Read tools are pre-selected. Anything else needs an explicit choice.

Paths below use `$CLAUDE_PLUGIN_ROOT` (the plugin folder). Run scripts with:

```bash
uv run --project "$CLAUDE_PLUGIN_ROOT" python "$CLAUDE_PLUGIN_ROOT/skills/portal-mcp-creator/scripts/<script>.py" ...
```

## Pipeline

### 1. Scope
Ask which service (its web address) and which parts of it the user needs. Get a short list of functions in the user's own words, for example "list my invoices and download the PDF".

### 2. Reconnaissance (no login)
```bash
uv run --project "$CLAUDE_PLUGIN_ROOT" python "$CLAUDE_PLUGIN_ROOT/skills/portal-mcp-creator/scripts/recon.py" https://<host> > recon.json
```
This looks for public API docs (OpenAPI/Swagger) and extracts endpoint candidates from the web app's JavaScript. Read the output. If there is an OpenAPI document, use it to name and type the endpoints later.

### 3. Static analysis
Use the `endpoints` in `recon.json` as hints for paths and parameters. Note endpoints that look like writes; they are out of scope for this version.

### 4. Login and recording
Write the manifest draft first (step 10 format, minimal: name, base_url, login_url, session, empty tools). Save it to `services/<name>.yaml` in the data directory (`portal-mcp` user data dir, or `PORTAL_MCP_HOME`). Then:

```bash
uv run --project "$CLAUDE_PLUGIN_ROOT" python "$CLAUDE_PLUGIN_ROOT/skills/portal-mcp-creator/scripts/record.py" \
  --service <name> --visit /path/to/page --visit /another/page --out <recording dir>
```

Tell the user a browser window is opening and that they should log in there (BankID, password or SSO, whatever the service uses). The script waits up to 10 minutes. After login it visits the paths you give. Ask the user which pages to visit if you do not know them.

The script prints a summary: which pages were visited and which requests were blocked, with reasons. Show the user the blocked list. If a read function needs a request that was blocked, say so and ask the user whether to visit it another way. Do not lift the guard.

### 5. Exploration
Repeat step 4 with more `--visit` paths when the user wants more functions. Pagination: visit a second page of a list if it exists, so the analysis sees `page` or `offset` parameters.

### 6. Analysis
```bash
uv run --project "$CLAUDE_PLUGIN_ROOT" python "$CLAUDE_PLUGIN_ROOT/skills/portal-mcp-creator/scripts/analyze.py" <recording dir>/recording.har --host <host> > catalog.json
```
The catalog holds structure only: path templates, query parameter names, pagination hints, response field names and types, and sensitive field names. It contains no values. Check its `tools` list against what the user asked for. Check `skipped` too.

### 7. Catalog
Show the user a short table: suggested name, kind (`read` or `file`), path, what it returns (field names only), and flags for sensitive fields. Mention any function that was found but is out of scope (writes).

### 8. The user's choice
Ask which tools to create. Read tools are pre-selected. Ask for a rate limit (default 30 per minute) if the user cares about it. Ask whether any read result should be cached (`cache_seconds`). Ask whether any field must be removed from responses (`redact`), and suggest the sensitive fields from the catalog.

### 9. Generation
Write the manifest. Descriptions are yours, written from the analysis, in the user's language. Use this shape:

```yaml
name: <service>
enabled: true
base_url: https://<host>
login_url: https://<host>/<login path>
profiles: [default]
session:
  type: cookies                 # or bearer, with bearer_source "localStorage:<key>"
  csrf: {header: <header>, from_cookie: <cookie>}   # only if the site needs it
  check: <path that returns 200 when logged in and 401 otherwise>
  logout: <path>                # optional
rate_limit_per_minute: 30
cache_seconds: 0
redact: [<dotted paths, e.g. items.owner.national_id>]
tools:
  - name: list_invoices
    kind: read
    description: <written by you>
    method: GET
    path: /api/invoices
    params:
      type: object
      properties:
        page: {type: integer}
    pick: [items.id, items.date, items.amount, items.status]
  - name: download_invoice_pdf
    kind: file
    description: <written by you>
    method: GET
    path: /api/invoices/{invoice_id}/pdf
    params:
      type: object
      properties:
        invoice_id: {type: string}
      required: [invoice_id]
```

Rules for the manifest:
- `path` uses `{name}` for path parameters. Each one must be in `params` and is required.
- `params` is a JSON Schema. Parameters that are not path parameters become query parameters on GET.
- `pick` keeps only the listed dotted fields. For a list response, prefix with the list key (`items.id`). Use it to keep the output small and to drop fields the user does not need.
- `session.check` must be an endpoint that returns 200 when logged in and 401 when not. If you cannot find one, ask the user for a page that needs login. Do not guess.
- Keep `name` and tool names to lowercase letters, digits and underscores.

Show the full manifest to the user and wait for approval. Save it only after approval.

### 10. Verification
Start the runtime tools (restart the client or use `/plugin` if the new tools do not show up; the runtime also reloads the manifest on change). Call each approved read tool once through MCP and check:
- the answer is JSON with the fields in the catalog,
- redacted fields are gone,
- for file tools, a file was saved and it starts with the expected bytes.

Report results to the user. If a tool fails, read the error, fix the manifest, and retry. Do not ask the user to share the response body; describe the field names instead.

Write tools: not in this version. The first real run of any future write tool is done together with the user.

### 11. Cleanup
Delete the recording directory. Then commit the manifest in the local manifest git repo (`git -C <services dir> add <name>.yaml && git -C <services dir> commit`, creating the repo with `git init` the first time). Never commit recordings or session files.

### 12. Summary
Tell the user which tools exist now, which were left out and why, and where the manifest is.

## Adding functions to an existing service
Run this pipeline again for the new parts only. Add new tools to the same manifest. Do not change existing tool names without asking, since clients may depend on them.

## Notes
- Recording is the only step that needs the user. Everything else runs without a login.
- If `record.py` reports that no browser is available, tell the user to install Google Chrome, or to run `uv run playwright install chromium`.
- If the login times out, ask the user whether to try again.
