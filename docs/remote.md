# Running the runtime on another machine

By default everything runs on your own computer. Claude Code starts the runtime itself with `uv run` and talks to it over stdio, so there is no network connection at all. The login window, the encrypted sessions, the encryption key in your system keyring and the confirmation page for write actions are all on that computer.

This page covers what works when the runtime runs somewhere else, and what does not yet.

| Where the runtime runs | Logged-in services | Public sites |
|---|---|---|
| Your own computer (default) | Yes | Yes |
| Another computer of yours with a desktop | Yes, with an SSH tunnel and screen sharing | Yes |
| A server or cloud machine without a desktop | No | Should work, not tested |

## Security first

HTTP mode has **no authentication**. Anyone who can reach its port can call every tool with your logged-in sessions, including preparing write actions. So:

- Keep the default `--host 127.0.0.1`. Never start it on `0.0.0.0` or another address that the network can reach.
- Reach it from another machine only through an SSH tunnel (or a similar encrypted tunnel that you control).
- Do not put it behind a public reverse proxy or a port forward on your router.

## Another computer of yours with a desktop

For example a computer at home that is always on, used from your laptop.

### 1. Set it up on the other computer

Install `uv`, Google Chrome and the plugin on the other computer, as described in [Install](../README.md#install).

The runtime keeps manifests, sessions and recordings in its own data directory on the machine where it runs (see [Where your data lives](../README.md#where-your-data-lives)). Add services on that computer, with `/portal-mcp:add` in Claude Code there, because the recording opens a browser window on that computer's screen. Manifests can be copied between machines. Sessions cannot: they are encrypted with a key in that computer's keyring.

### 2. Start the runtime in HTTP mode

Start it **from the desktop session** of the other computer, not from a plain SSH login. The login window must open on a screen, and on macOS the keychain is often locked in SSH sessions. For example, in a terminal on the other computer, or through screen sharing:

```bash
uv run --quiet --project /path/to/portal-mcp portal-mcp-runtime --http
```

It listens on `http://127.0.0.1:8765/mcp`. Use `--port` for another port.

### 3. Open a tunnel from your laptop

```bash
ssh -N -L 8765:127.0.0.1:8765 you@other-computer
```

This makes the other computer's port 8765 reachable as `127.0.0.1:8765` on your laptop, encrypted, and only for you.

### 4. Connect Claude Code on your laptop

```bash
claude mcp add --transport http portal-mcp-remote http://127.0.0.1:8765/mcp
```

The tools then come from the other computer. If the plugin is also installed on your laptop, its local runtime runs too, with its own services; the two have different tool names (`mcp__plugin_portal-mcp_portal-mcp__…` for the local one, `mcp__portal-mcp-remote__…` for the remote one).

### 5. Logging in

When a tool needs a login, the browser window opens **on the other computer's screen**, and the desktop notification appears there too. Open screen sharing to it (on macOS: Screen Sharing, or any remote desktop tool), log in in the window as usual, and call the tool again.

Sessions are then stored on the other computer and are reused until they expire.

### 6. Approving write actions

A write action needs your approval. When your client supports MCP elicitation (Claude Code does), the question is asked in Claude Code on your laptop. Otherwise the runtime opens a confirmation page in a browser **on the other computer**, on a random local port, so you answer it through screen sharing. Approval over a tunnel has not been tested with every client; check it once with a harmless write before you rely on it.

## A server or cloud machine without a desktop

Logins are refused there on purpose. You always log in yourself, in a visible browser window, and the runtime never handles passwords. A machine without a desktop cannot show that window, so the runtime answers that a login needs a graphical desktop (on Linux it looks for `DISPLAY` or `WAYLAND_DISPLAY`).

Public sites (`session: {type: none}`) need no login and no stored session, so they should work on such a machine with HTTP mode and a tunnel as above. This has not been tested yet. Adding a public service still needs a browser for the recording, so add it on a computer with a desktop and copy the manifest over.

## Known limits

- **Services may treat a data centre as suspicious.** Many services block or question logins and traffic from cloud addresses, or ask for extra verification.
- **Device-bound sign-ins.** BankID and "remember this device" are often tied to one device. A session from one computer may not be accepted from another, so log in on the computer that runs the runtime.
- **One user.** The runtime holds one person's sessions. Do not share one runtime between several people.

## What real cloud use would need (future work)

None of this exists yet:

- **Remote login.** The runtime has an interface for login providers, but only the local one is implemented. A remote provider could run the browser on the server and show its window to you through a secure, short-lived link, so that you still log in yourself.
- **A session key that is not tied to one keyring.** For example a key you hold, or one from a secrets manager.
- **Authentication for HTTP mode,** so that it could listen on more than a loopback address. Until then, the runtime should refuse to listen on other addresses.
