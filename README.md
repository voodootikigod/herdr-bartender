# Herdr Bartender Plugin

A native Herdr plugin that bridges Herdr's pane lifecycle and agent status events directly into **Bartender Pro's Top Shelf** (NotchBar AI Agent bridge) on macOS.

## Features

- **Accurate State Synchronization**:
  - `blocked` &rarr; `Waiting` (Triggers Top Shelf's notch highlight: *"Awaiting your input / Agent needs attention"*).
  - `working` &rarr; `Working` (Displays active turn/tool spinner).
  - `done` &rarr; `Done` (Shows task completion indicator, auto-dismissed by Bartender).
  - `idle` &rarr; `Idle`.
  - `pane.closed` / `tab.closed` / `workspace.closed` &rarr; `Ended` (Prunes session from Top Shelf).
- **Dynamic Agent Attribution**: Formats agent names as `<Agent> (Herdr)` (e.g., `Claude (Herdr)`, `Codex (Herdr)`).
- **Zero Dependencies**: Pure Python 3 script using built-in macOS standard libraries (`urllib`, `json`).
- **Remote Host Support**: Configurable host/port for remote machines (like `dev-one`) using SSH reverse tunnels or Tailscale.

## Installation

1. Clone or place this repository at `~/Projects/herdr-bartender`.
2. Link it into Herdr's plugin directory:
   ```bash
   mkdir -p ~/.config/herdr/plugins/local
   ln -s ~/Projects/herdr-bartender ~/.config/herdr/plugins/local/herdr-bartender
   ```
3. Register the plugin in `~/.config/herdr/plugins.json` (or restart Herdr).

## Remote Sessions (e.g., `dev-one`)

When running Herdr on a remote host (e.g., `herdr --remote dev-one.taildd69d8.ts.net`), the remote Herdr instance needs to reach Bartender Pro on your local Mac.

### Method 1: SSH Reverse Port Forwarding (Recommended)
Add this to your `~/.ssh/config` on your local Mac:

```ssh
Host dev-one dev-one.taildd69d8.ts.net
    RemoteForward 7823 127.0.0.1:7823
```

This forwards `localhost:7823` on the remote server directly back to your Mac's Bartender Top Shelf bridge. Install `herdr-bartender` on the remote server, and it works immediately without changing any ports.

### Method 2: Tailscale / Network IP
Alternatively, set the environment variables in your remote shell or Herdr environment:
```bash
export NOTCHBAR_AGENTS_HOST="<your-mac-tailscale-ip>"
export NOTCHBAR_AGENTS_PORT="7823"
```

## CLI Commands & Diagnostics

```bash
# Check if Bartender Top Shelf bridge is running and reachable
./bin/herdr-bartender --health

# Run an interactive simulation of Working -> Waiting -> Done -> Ended
./bin/herdr-bartender --test

# List currently tracked sessions
./bin/herdr-bartender --sessions

# Force-clear any stale sessions from Top Shelf
./bin/herdr-bartender --cleanup
```
