# portal-mcp

Create MCP tools for logged-in web services and customer portals that have no open API.

- You log in yourself, in a visible browser window. The runtime never reads or stores your password.
- Each service is described by a YAML manifest. The runtime turns the manifest into MCP tools.
- Sessions are stored encrypted. The encryption key lives in your system keyring.
- Write actions will need a human confirmation (phase 3, not in this version yet).

**Status: phase 1.** The runtime, the `local` login provider, encrypted session storage, read and file tools, and a local mock service with tests are in place. The creator skill, the slash commands, write tools and `self_test` come in later phases.

## Requirements

- A computer with a graphical desktop: macOS, Windows, or Linux with a desktop environment.
- [`uv`](https://docs.astral.sh/uv/) to run the runtime.
- Google Chrome (preferred), or Playwright's Chromium: `uv run playwright install chromium`.

## Install as a private plugin

```text
/plugin marketplace add markstrom/portal-mcp
/plugin install portal-mcp@portal-mcp
```

## How it fits together

```mermaid
flowchart LR
    client["MCP client<br/>(Claude Code)"] -- "stdio or localhost HTTP" --> runtime["portal-mcp runtime"]
    runtime --> manifests["services/*.yaml"]
    runtime --> store["sessions/ (encrypted)"]
    runtime -- "HTTP with the session" --> service["The service's web API"]
    runtime -- "opens and watches" --> browser["Visible browser window<br/>(you log in here)"]
    creator["portal-mcp-creator skill"] -- "writes manifests" --> manifests
    creator -- "guided recording" --> browser
```

The skill creates manifests. The runtime turns them into tools. The browser is only used for logging in and recording, and you control the login.

## Login and a tool call

```mermaid
sequenceDiagram
    participant C as MCP client
    participant R as Runtime
    participant K as Session store
    participant B as Browser window
    participant S as Service
    C->>R: call example_list_invoices
    R->>K: load session
    alt no valid session
        R->>B: open login_url, notify the user
        B->>S: user logs in
        R->>S: poll session check until 200
        R->>K: save encrypted session
        R-->>C: "log in and call again" (if not done within ~20 s)
    end
    R->>S: GET /api/invoices with session
    S-->>R: JSON
    R->>R: pick fields, redact, cache
    R-->>C: result
```

## Creating a service (skill pipeline)

```mermaid
flowchart TD
    A[Scope: which service and functions] --> B[Recon without login]
    B --> C[Guided recording<br/>you log in, GET only after login]
    C --> D[Analysis: path templates, fields, pagination]
    D --> E[Catalog: read / file, sensitive fields]
    E --> F{You choose tools}
    F --> G[Generate manifest, you approve]
    G --> H[Verify tools against recording]
    H --> I[Delete recordings, commit manifest]
```

## Where your data lives

Data is kept outside the plugin, so updates never remove it. The location follows your OS conventions (`platformdirs`):

```text
<user data dir>/portal-mcp/
  services/<service>.yaml     manifests
  profiles/<service>/<profile>/  browser profiles
  sessions/                   encrypted sessions
  recordings/                 temporary recordings (later phases)
```

Set `PORTAL_MCP_HOME` to use another base directory. Downloaded files go to your Downloads folder, or to `PORTAL_MCP_DOWNLOADS` if set.

## Manifest

One file per service, `services/<name>.yaml`. The file name must match `name`. A broken manifest only affects its own service.

```yaml
name: example
enabled: true
base_url: https://app.example.com
login_url: https://app.example.com/login
profiles: [default]
session:
  type: cookies               # cookies | bearer
  bearer_source: null         # for bearer: "localStorage:<key>" or "sessionStorage:<key>"
  csrf:
    header: x-csrf-token
    from_cookie: csrf
  check: /api/session         # 200 = logged in, 401 = not logged in
  logout: /api/logout         # optional, called with POST
rate_limit_per_minute: 30
cache_seconds: 30             # caches read results
redact: [items.owner.national_id]
tools:
  - name: list_invoices
    kind: read                # read | file (write and binding come in phase 3)
    description: List invoices with date, amount and status.
    method: GET
    path: /api/invoices
    params:
      type: object
      properties:
        page: {type: integer}
    pick: [items.id, items.date, items.amount, items.status]
  - name: download_invoice_pdf
    kind: file
    description: Download the PDF for one invoice.
    method: GET
    path: /api/invoices/{invoice_id}/pdf
    params:
      type: object
      properties:
        invoice_id: {type: string}
      required: [invoice_id]
```

Tool names are prefixed with the service name, for example `example_list_invoices`. Every tool also takes an optional `profile` argument.

Common tools: `list_services`, `forget_session(service)`, `logout(service)`.

### Logging in

When a tool needs a session and none is valid, a browser window opens on `login_url` and a desktop notification asks you to log in. The runtime checks the session check endpoint and captures the session once you are logged in. A tool call waits up to about 20 seconds; if you are not done by then, it tells you to finish in the window and call again. The login continues in the background. Only one login runs at a time.

## Development

```bash
uv sync
uv run pytest
```

The tests run against `tests/mock_service/`, a local fake portal with invented data. Tests that need a browser are skipped if neither Google Chrome nor Playwright's Chromium is installed.

Run the server directly:

```bash
uv run portal-mcp-runtime            # stdio (default)
uv run portal-mcp-runtime --http     # streamable HTTP at http://127.0.0.1:8765/mcp
```

Logs go to stderr only.

## Security notes

- Passwords are never handled by this software. Logging in happens in a window you can see.
- Session files are encrypted with a key stored in the system keyring and have mode 0600.
- The HTTP transport only binds to localhost.
- Read results can be trimmed with `pick` and `redact` before they reach the model.
