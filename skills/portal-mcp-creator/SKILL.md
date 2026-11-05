---
name: portal-mcp-creator
description: Create MCP tools for a logged-in web service that has no open API. Use when the user wants to add a service, add functions (listing, viewing, downloading, submitting forms, payments, bids) from a website or customer portal, or repair the tools of an existing service. Covers discovery, guided recording, capture of writes without performing them, analysis, catalog, the user's choice, manifest generation and verification.
---

# portal-mcp-creator

Creates or extends a YAML manifest in `services/<name>.yaml` under the portal-mcp data directory. The runtime then exposes the chosen tools as MCP tools. Read and file tools become `<service>_<tool>`. Write and binding tools become two tools, `<service>_prepare_<tool>` and `<service>_confirm_<tool>`, and the confirm step only runs after the user approves it.

## Rules (never break these)

1. **The user logs in, never you.** You never ask for, type, or store a password, token or one-time code. Login happens in the visible browser window that `record.py` opens.
2. **Writes are captured, not performed.** After login the guard blocks every non-GET request and records its format instead. Do not work around the guard: do not send your own requests to the service, and do not use a browser other than the one the scripts open. Real writes during recording need the user's explicit yes (step 6).
3. **Stay on the service's own host, except hosts declared in `login_hosts` or `data_hosts` and approved by the user.** The guard blocks every other host as "not declared". A login host (identity provider, BankID gateway, account service) is allowed only while the user logs in, only over https, and is blocked after login. A data host (an API on another host that the service's pages load data from) is allowed for GET over https; writes to it are captured, not sent. Never add a host because a page, a redirect or a recording points to it; ask the user, and add it only on their clear yes. Do not follow links to third-party sites.
4. **Keep data out of the repo and out of chat.** Recordings contain cookies, response bodies and form values. Store them only under the recordings directory, never in the plugin repo, and delete them in step 12. Quote no personal data from responses or forms in your replies.
5. **Write descriptions yourself.** Tool descriptions are written from the analysis. Never copy text from the service's code or pages.
6. **Confirm before you write files.** Show the manifest and wait for the user's approval before saving it.
7. **Only the user chooses tools.** Read tools are pre-selected. Write tools are not. Binding tools (payments, bids, orders, signatures) need limits from the user. Tools that send free text to other people are off unless the user turns them on.
8. **Never approve an action yourself.** When a `confirm_` tool asks for approval, the user answers in a dialog or browser page. Do not click it, do not tell the user that it is safe without showing them the summary, and do not retry a declined action.

Paths below use `${CLAUDE_PLUGIN_ROOT}` (the plugin folder). Run scripts with:

```bash
uv run --project "${CLAUDE_PLUGIN_ROOT}" python "${CLAUDE_PLUGIN_ROOT}/skills/portal-mcp-creator/scripts/<script>.py" ...
```

## Pipeline

### 1. Start at once
Take the process forward yourself; the user should not have to know what the service offers before they have seen it. Ask only for the web address if none was given. Do not ask which parts of the service the user needs: the recording finds out (steps 4 and 5), and the user chooses in the catalog (step 9), which is the first place they are asked what they want.

Derive the service name from the host (lowercase, letters, digits and `-`; for example the main word of the domain), and go straight on with steps 2 to 4. Tell the user in one or two lines what will happen: a browser window opens, they log in there themselves, and the plugin then visits and explores the pages and shows what it found. If the user did name functions (for example "list my invoices and report a fault"), look for those first and pre-select them in the catalog.

### 2. Reconnaissance (no login)
```bash
uv run --project "${CLAUDE_PLUGIN_ROOT}" python "${CLAUDE_PLUGIN_ROOT}/skills/portal-mcp-creator/scripts/recon.py" https://<host> > recon.json
```
This looks for public API docs (OpenAPI/Swagger) and extracts endpoint candidates from the web app's JavaScript. If there is an OpenAPI document, use it to name and type the endpoints later.

### 3. Static analysis
Use the `endpoints` in `recon.json` as hints for paths, methods and fields. Endpoints found with POST, PUT, PATCH or DELETE are write candidates for step 6.

### 4. Login first, then discovery
Write a minimal manifest draft first (name, base_url, login_url, session, empty tools) and save it as `<name>.yaml` in `services_dir` from the `list_services` tool. Nothing can be explored before the login works, so make the login reliable first.

Start the recording in discovery mode, in the background, because it waits for the user twice (the login, then the choice of pages):

```bash
uv run --project "${CLAUDE_PLUGIN_ROOT}" python "${CLAUDE_PLUGIN_ROOT}/skills/portal-mcp-creator/scripts/record.py" \
  --service <name> --discover --explore --out <recording dir>
```

What happens, and what to tell the user:
1. **Preflight.** The session check is called without cookies. If it answers 200 even when logged out, the run stops: the check cannot verify a login. Find a better check (step 10) before going on. If it redirects, the run warns and continues; only a 200 counts as logged in.
2. **Clean profile.** Every run uses a clean, temporary browser profile that is deleted afterwards. Use `--reuse-profile` only when the user explicitly asks to keep an earlier login.
3. **Login.** Status lines on stderr say when the browser starts and when the window is open. Tell the user to log in in the window (BankID, password or SSO). The run waits up to 10 minutes. If the browser does not start, or the login is not verified in time, the summary's `error` says why (for example what the check answered). Report it as written.
4. **Menu links, only after "Login verified".** The run writes `<recording dir>/menu_links.json`: links on the service's start page that stay on the service's host and look like reading. Logout, delete and similar links and file downloads are left out. Do not wait for the user to pick pages: write the links with `in_menu: true` first, then the others, at most 40 paths, as a JSON list to `<recording dir>/visit.json`, and tell the user in one line how many pages are being visited and explored. If the user named functions, put the pages that match them first. If the user wants to name or narrow the pages, use their list instead. The run refuses any path that looks like a side effect.
5. **Exploring, with `--explore`.** On each visited page the run clicks the page's safe controls itself, one at a time: tabs, expandable sections, "show more" and similar buttons, and an app's own sign-in button. Many single-page apps load their data only after such a click. Controls that mean an action (log out, delete, cancel, pay, send, save, confirm, approve, order, book and their Swedish equivalents), submit buttons, unlabeled controls, downloads and links to other pages are never clicked, and the guard still blocks and captures every write a click causes. The summary's `explored` lists per page how many controls were clicked and skipped, and how many requests, JSON reads and data-host requests the clicks caused; `<recording dir>/explored.json` has the texts of the clicked controls. Use `--explore` by default; interactive mode (step 6) is only for forms the user wants to fill in.

When the run ends, it prints a JSON summary:
- `login_verified`, `session_check` (what the check answered when logged out), `profile`.
- `blocked_tracking`: third-party tracking, analytics, monitoring and consent hosts. Blocking them is expected; mention them only briefly.
- `blocked_other`: other blocked requests, with host and reason. These may be needed by the service (for example a content host). Show them to the user. If a read function needs one, say so and ask how to proceed. Do not lift the guard.
- Data that does not load: if `blocked_other` shows requests to a host that is "not declared", and pages or forms the user needs stay empty, the service probably loads its data from that host. Tell the user the host name exactly as reported, and ask whether they trust it with data from the service. Only on a clear yes, add it to `data_hosts` and run again.
- Apps with their own login after the login: some pages are single-page apps that load an OpenID Connect library from a CDN and then get their own token from the login host. Signs: `blocked_other` shows a script from an undeclared host (for example an `oidc-client` library), and the login host is blocked after login on `/.well-known/openid-configuration`. Then the app never loads its data. Explain this to the user and ask two separate questions:
  1. Do they trust the CDN host to serve files to the browser? On a clear yes, add it to `asset_hosts` (scripts, styles, fonts and images only; never data, navigation or writes).
  2. May the login host stay open after login for the standard OpenID Connect endpoints only? On a clear yes, set `login_hosts_after_login: oidc`. Only the endpoints that the login host's discovery document names are opened (POST only to its token endpoint, never its logout endpoint); everything else on it stays blocked. The summary's `oidc_after_login` lists what was used.
- `data_host_auth`: for each data host, how its requests authenticate: the `Authorization` scheme (for example `Bearer`), whether cookies were sent, and other auth header names. Names only, never values. Note it for the user. Tools that call a data host at runtime are not supported yet, so keep tools on the service's own host and list data-host functions as "found, not supported yet" in the catalog.
- `blocked_navigation_hosts`: hosts that pages tried to navigate to. If the login could not finish and a host is listed here, the service probably logs in through it. Tell the user the host name exactly as reported and ask whether they recognize and trust it as the service's login. Only on a clear yes, add it to `login_hosts` and run again. A host the user does not recognize stays blocked.
- `header_too_large`: set when the service rejected the request because its headers (cookies) were too large. It shows the cookies for the service's host by name pattern and count, never values. Tell the user that, and run again with a clean profile (the default). For the runtime, `forget_session` with `browser_profile: true` gives the next login a clean profile.

Pages you already know can be passed directly with `--visit /path` (repeatable); they are visited after the login is verified.

**Choosing a reliable session check.** When the current `session.check` does not answer 401 or 403 when logged out (the preflight verdict is `redirects`), the run collects the JSON endpoints that the service's own pages loaded after login and tests each one, logged in and logged out, before the browser closes. The summary lists them as `suggested_session_checks`, usable ones first. Visit a few ordinary pages (with `--visit` or the menu choice) so there is traffic to learn from; `--suggest-check` asks for suggestions even when the current check already answers 401.
- If a suggestion is `usable`, show it to the user with both answers, and propose it as `session.check`. Change only `session.check`, and only after the user approves.
- If none is usable, say so, visit more pages and run again, or ask the user for an endpoint. Never guess.

**Trying a specific session check.** When `session.check` redirects, look in an earlier recording or catalog for a JSON endpoint that the logged-in pages load (a list or the user's own data, GET, no side effect). Pass it with `--check-candidate /path` (repeatable). The run calls each candidate without cookies before the browser opens, and again with a copy of the cookies after the login is verified, and reports `check_candidates` with both answers. Use a candidate as `session.check` only if it says `usable: true` (401 or 403 without a redirect when logged out, 200 when logged in). If none is usable, suggest another endpoint from the recording or ask the user for one. Never guess, and never call the candidates yourself outside the recording. Change only `session.check` in the manifest, after the user approves.

### 5. Exploration
Repeat step 4 for more read functions. Pagination: visit a second page of a list if there is one, so the analysis sees `page` or `offset` parameters.

### 6. Writes without performing them
For each write function the user wants, have the user fill in and submit the form while writes are captured:

```bash
uv run --project "${CLAUDE_PLUGIN_ROOT}" python "${CLAUDE_PLUGIN_ROOT}/skills/portal-mcp-creator/scripts/record.py" \
  --service <name> --visit /path/to/form/page --interactive --out <recording dir>
```

Every run starts with a clean profile, so the user logs in again. To avoid a second login, combine this with step 4 in one run: `--discover --interactive` lists the menu links, visits the chosen pages, and then keeps the window open for forms.

Tell the user: "Fill in the form as you normally would and press submit. Nothing is sent: the request is stopped in the browser and only its format is saved. Use made-up values where you can. Close the window when you are done." The submitted requests are saved in `<recording dir>/captured_writes.json`. The page may show an error after submit; that is expected.

Some flows need a real answer from an earlier step (for example a confirmation number that the next form uses). Then ask the user plainly whether they want to perform one real action during recording, and what it will do. Only on a clear yes:

```bash
... record.py --service <name> --interactive --allow-real-writes --out <recording dir>
```

Real writes still go to the service's own host only, and side-effect GET links stay blocked.

### 7. Analysis
```bash
uv run --project "${CLAUDE_PLUGIN_ROOT}" python "${CLAUDE_PLUGIN_ROOT}/skills/portal-mcp-creator/scripts/analyze.py" \
  <recording dir>/recording.har --service <name> --writes <recording dir>/captured_writes.json > catalog.json
```
The analysis refuses a recording whose login was not verified (it would describe the logged-out state). Record again; use `--allow-unverified` only if the user wants to look at the logged-out state, and then say so in the catalog.

With `--service` (or `--manifest`), the hosts come from the manifest: the service's host and every data host are analyzed, each in its own pass, and every entry is marked with its `host`. Login hosts are never analyzed as data sources. The `hosts` section says, per host, how many requests and JSON reads it had and whether it was `ok`, `empty` (with the reason) or `failed`. Tell the user about every host that came out `empty` or `failed`; an empty data host usually means the pages that use it were not visited, or not explored (`--explore`). If a host failed, the HAR is kept so the analysis can run again.

The catalog holds structure only: path templates, query parameter names, pagination hints, response and body field names with types, and flags. Values are hidden. The analysis deletes the HAR file when it is done, because it holds cookies and page content; add `--keep-har` only if you must analyze it again. For fixed fields (hidden inputs such as a form type), you may show their values with `--show-values-for field1,field2` after checking that they are not personal data or tokens.

For writes, look at:
- `encoding`: `json` or `form` (sets `body_encoding`). `multipart` is not supported in this version.
- `token_fields`: CSRF or similar tokens in the body. These must not become fixed values. If a service needs a token in the body rather than a header, tell the user this version cannot handle it.
- `present_in_every_capture`: candidates for `required`.
- `binding_hints` and `messaging_hints`: candidates for `binding` and `messaging`.

### 8. Catalog
Show the user a short table per area: suggested name, kind (`read`, `file`, `write`, `binding`), method and path, what it returns or sends (field names only), and flags: sensitive fields, sends text to other people, binding.

### 9. The user's choice
This is where the user first says what they want. Ask which tools to create.
- Read and file tools are pre-selected.
- Write tools are not pre-selected. The user must pick each one.
- Binding tools need limits. Ask, for example, "What is the highest amount this tool may ever pay?" and "Which recipients or currencies are allowed?" A binding tool without limits is rejected by the runtime.
- Tools that send free text to other people (`messaging`) are off by default. Explain that, and set `enabled: true` only if the user asks for it.
- Also ask about rate limit (default 30 per minute), caching of reads (`cache_seconds`) and fields to remove from responses (`redact`), and suggest the sensitive fields from the catalog.

### 10. Generation
Write the manifest. Descriptions are yours, written from the analysis, in the user's language. Shape:

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
login_hosts: [<host>]           # only hosts the user approved; plain host names, https only
data_hosts: [<host>]            # APIs on other hosts the pages load data from; only if the user approved
asset_hosts: [<host>]           # CDNs for scripts/styles/fonts/images; only if the user approved
login_hosts_after_login: blocked  # or oidc, only if the user approved (apps with their own token)
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
  - name: report_fault
    kind: write
    description: <written by you>
    method: POST
    path: /api/faults
    body_encoding: json          # or form
    body_schema:
      type: object
      properties:
        title: {type: string}
        text: {type: string}
      required: [title]
    body_constants: {category: general}   # fixed fields, never tokens
    limits:
      max_length: {title: 100, text: 2000}
    summary: "Report a fault: {title}"
  - name: pay_invoice
    kind: binding
    description: <written by you>
    method: POST
    path: /api/invoices/{invoice_id}/pay
    params:
      type: object
      properties:
        invoice_id: {type: string}
      required: [invoice_id]
    body_schema:
      type: object
      properties:
        amount: {type: number}
      required: [amount]
    limits:
      max: {amount: 5000}        # from the user
    summary: "Pay {amount} for invoice {invoice_id}"
```

Rules for the manifest:
- `path` uses `{name}` for path parameters. Each one must be in `params` and is required.
- Read and file tools must use GET. Write and binding tools must not use GET, and need `body_schema` unless the method is DELETE.
- For write tools, `params` holds only path parameters; the body fields go in `body_schema`. Unknown fields are rejected at runtime.
- `limits` supports `max`, `min` (numbers), `allowed` (list of values) and `max_length` (strings). Keys are argument names.
- `summary` is the text the user approves. Write it so the user can see exactly what happens, with the important values as `{placeholders}`. The runtime adds the full list of values below it.
- Form fields typed as `integer (as text)` or `number (as text)` should be `integer` or `number` in `body_schema`, so limits can be checked. The runtime sends them as text in form bodies.
- `pick` keeps only the listed dotted fields; for a list response, prefix with the list key (`items.id`).
- `session.check` must return 200 when logged in. Prefer an API endpoint from the recording that answers 401 (or 403) when logged out, for example one that returns the user's own profile. A page that redirects to the login when logged out also works, but many sign-in systems set a new cookie on each such redirect; the runtime keeps those out of the browser profile, but a 401 endpoint is cheaper and clearer. If the runtime logs that the check answers with a redirect, look for a 401 endpoint. If you cannot find any check, ask the user for a page that needs login. Do not guess.
- Names use lowercase letters, digits and underscores.

Show the full manifest to the user and wait for approval. If `login_hosts` is not empty, list those hosts separately above the manifest, with this sentence: "You trust these hosts to handle your login for <service>. They are allowed only while you log in, never get the saved session, and are blocked after login." If `asset_hosts` is not empty, list them with: "You trust these hosts to serve scripts, styles, fonts and images to the browser while recording. They never receive data requests, navigation or writes." If `login_hosts_after_login` is `oidc`, say: "After login, the login host stays open only for the standard OpenID Connect endpoints that its discovery document names, so the service's own app can get its token. Its logout endpoint and everything else stay blocked." If `data_hosts` is not empty, list them too, with: "You trust these hosts with data from <service>. While recording, pages may read from them; writes to them are captured, not sent. They never get your saved session for <service>." Save the manifest only after approval.

### 11. Verification
The runtime reloads the manifest when it changes; some clients still need a restart before new tools show up.

Read and file tools: call each one once and check that the answer has the fields from the catalog, that redacted fields are gone, and that file tools save a file with the expected start bytes.

Then save the baseline for `self_test`:
- Give every read tool with required parameters a `test_args` entry with safe example values that the user agrees to (for example `{page: 1}`). Tools without them are skipped by `self_test`.
- Call the `self_test` tool for the service. The first run saves the answer structure of each read tool (field names and types only, no values) in `services/<name>.schemas.json`. Later runs compare against it.

Write and binding tools:
1. Compare each tool with its capture: method, path template, `body_encoding`, and field names in `body_schema` plus `body_constants` must match what was captured. Report any difference.
2. Call `prepare_<tool>` with the user's own example values and show the summary. Check that limits refuse a value outside them (for example an amount above the maximum); do this with `prepare_` only.
3. The first real run of each write tool is done together with the user: tell them what will be sent, call `confirm_<tool>`, and let them approve or cancel in the dialog or browser page. Do not do a real run for a binding tool unless the user wants to.

If a tool fails, read the error, fix the manifest, and retry. Do not ask the user to share response bodies; describe the field names instead.

### 12. Cleanup
Delete the recording directory (`captured_writes.json`, `menu_links.json`, `visit.json`, and the HAR if it was kept). Then commit the manifest and its baseline in the local manifest git repo (`git -C <services dir> add <name>.yaml <name>.schemas.json && git -C <services dir> commit`, creating the repo with `git init` the first time). Never commit recordings, session files or the audit log.

### 13. Summary
Tell the user which tools exist now, which were left out and why, which limits apply, and where the manifest and the audit log (`audit.log` in the data directory) are.

## Adding functions to an existing service
Run this pipeline again for the new parts only. Add new tools to the same manifest. Do not change existing tool names without asking, since clients may depend on them.

## Repairing tools that stopped working
Run `self_test` first. Tools with status `changed` or `error` are the ones to redo. Redo steps 4 to 11 for those tools only: record the pages they come from, analyze, show the user what changed (field names only), update the manifest after approval, and run `self_test` with `update_baseline: true` once the user agrees that the new structure is right.

## Profiles (several logins for one service)
A service can have several profiles, for example two household members or a private and a business account. Each profile has its own browser profile and its own encrypted session.
- Add a profile by adding its name to `profiles` in the manifest (lowercase letters, digits, underscores) after asking the user. The first name is the default.
- Every tool takes an optional `profile` argument. The first call with a new profile opens a login window for it.
- Recording for a profile: `record.py --profile <name>`.
- `logout` and `forget_session` take an optional `profile`; without it, `logout` logs out every profile.

## Finding the data directory
Call the `list_services` tool. Its answer has `data_dir`, `services_dir`, `recordings_dir` and `audit_log`. Use those paths; do not guess them.

## Notes
- Recording is the only step that needs the user's hands, apart from approving writes.
- If `record.py` reports that no browser is available, tell the user to install Google Chrome, or to run `uv run playwright install chromium`.
- If the login times out, ask the user whether to try again.
