# Copilot Matrix Bridge

Chat with [GitHub Copilot CLI](https://githubnext.com/projects/copilot-cli/) from **Matrix/Element** — on your phone, tablet, or any device.

Each machine runs a local bridge that connects your Copilot CLI to Matrix rooms. Create a room, invite the bot, pick a project, and start chatting. Copilot sessions persist across messages.

## How It Works

```
Element (phone / desktop / web)
    ↕  Matrix protocol
Your Synapse Server
    ↕  matrix-nio
Bridge (this script, runs locally per device)
    ↕  subprocess
copilot --resume=<session> -p "msg" --yolo --output-format json
```

- Each Matrix room = independent Copilot session
- Tool usage steps stream to Matrix in real-time (🔧 Running tests, 📄 Reading file...)
- Copilot can ask you questions — they appear in chat, you reply, it continues
- Room names auto-update to reflect the session (e.g. `Fix Auth Bug | Portal-Main | development | 2026-04-05`)

## Prerequisites

- **GitHub Copilot CLI** installed and authenticated (`copilot` command working in your terminal)
- **Python 3.11+**
- A **Matrix/Synapse server** you control (or any Matrix homeserver where you can register bot users)
- **Element** (or any Matrix client) on the device you want to chat from

---

## Quick Start (Standalone)

### 1. Create a bot user on your Synapse server

```bash
register_new_matrix_user -u copilot-win -p <password> -c /path/to/homeserver.yaml --no-admin
```

Use different names per device: `copilot-win`, `copilot-mac`, `copilot-work`, etc.

### 2. Clone and configure

```bash
git clone https://github.com/elpandaios/copilot-matrix-bridge.git
cd copilot-matrix-bridge

cp config.yaml.example config.yaml
cp .env.example .env
```

Edit **`.env`** with your Matrix credentials:

```env
MATRIX_HOMESERVER=https://your-matrix-server.com
MATRIX_BOT_USER=@copilot-win:your-server.com
MATRIX_BOT_PASSWORD=your-bot-password
MATRIX_OWNER_ID=@your-username:your-server.com
```

Edit **`config.yaml`** for your machine:

```yaml
# Windows
device_name: "Windows Desktop"
projects_root: "C:\\Users\\you\\Projects"
copilot_command: "copilot"          # or full path: "C:\\nvm4w\\nodejs\\copilot.cmd"
copilot_timeout: 10800              # 3 hours

# macOS / Linux
device_name: "MacBook Pro"
projects_root: "/Users/you/Projects"
copilot_command: "copilot"
copilot_timeout: 10800
```

> **Windows note:** If `copilot` isn't on Python's PATH, use the full path to `copilot.cmd` (e.g. from nvm or Node.js install).

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run

```bash
python bridge.py
```

You should see:
```
🤖 Copilot Matrix Bridge starting...
   Device: Windows Desktop
   Projects: C:\Users\you\Projects
   Available projects: my-app, api-server, ...
```

---

## Quick Start (Docker)

Docker includes **E2E encryption support** (via libolm) and bundles Node.js + Copilot CLI.

### 1. Configure

Same as above — create `.env` and `config.yaml`. In `config.yaml`, set:

```yaml
projects_root: "/projects"   # Docker mount path
copilot_command: "copilot"
```

### 2. Run

```bash
docker compose up -d
```

The `docker-compose.yml` mounts your local project directories into the container. Edit the volume path to match your machine:

```yaml
volumes:
  - /Users/you/Projects:/projects    # macOS/Linux
  # - C:/Users/you/Projects:/projects  # Windows
```

### 3. Authenticate Copilot inside the container

```bash
docker compose exec bridge copilot auth
```

---

## Usage

### In Element

1. Create a new room (e.g. "Fix auth bug")
2. Invite your bot (e.g. `@copilot-win:your-server.com`)
3. Bot joins and greets you
4. Set a project: `:project my-app`
5. Start chatting!

### Commands

| Command | Description |
|---------|-------------|
| `:project <name>` | Set working directory |
| `:projects` | List available projects |
| `:mode <chat\|plan\|auto>` | Set room mode |
| `:session` | Show copilot session details |
| `:resume` | List & switch to past sessions |
| `:status` | Show current state |
| `:reset` | Start fresh session (new ID) |
| `:clear` | Clear copilot context (same session) |
| `:shutdown` | Kill stuck copilot processes |
| `:help` | Show all commands |

> Commands use `:` prefix (not `/`) to avoid conflicts with Element's built-in slash commands.

### Modes

| Mode | Behavior | Example |
|------|----------|---------|
| `chat` (default) | Conversational — answers and stops | "What does this function do?" |
| `plan` | Planning — structured plans, no execution | "Design the auth system" |
| `auto` | Autopilot — keeps working until done | "Implement the rate limiter" |

### Inline Prefixes

Override the room mode for a single message:

```
plan: design the database schema     → plan mode for this message
do: fix the failing tests            → autopilot for this message
```

### Multiple Chats

Each room is an independent copilot session:

- Room "Fix auth" → my-app → Session A
- Room "Add logging" → my-app → Session B  
- Room "Pricing page" → frontend → Session C

### Multi-Device

Run one bridge per machine. Each gets its own bot user:

| Device | Bot User | Runs locally |
|--------|----------|-------------|
| Windows Desktop | `@copilot-win:server.com` | `python bridge.py` |
| MacBook | `@copilot-mac:server.com` | `python bridge.py` |
| Work Laptop | `@copilot-work:server.com` | `python bridge.py` |

Invite the right bot into a room to control which device runs the work.

---

## Run as a Service (Optional)

### Windows — Task Scheduler

Create a scheduled task that runs at login:

| Field | Value |
|-------|-------|
| Program | `python` |
| Arguments | `bridge.py` |
| Start in | `C:\Users\you\...\copilot-matrix-bridge` |

### macOS — launchd

Create `~/Library/LaunchAgents/com.copilot-bridge.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.copilot-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>python3</string>
        <string>/Users/you/copilot-matrix-bridge/bridge.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/you/copilot-matrix-bridge</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.copilot-bridge.plist
```

### Linux — systemd

```ini
# ~/.config/systemd/user/copilot-bridge.service
[Unit]
Description=Copilot Matrix Bridge

[Service]
WorkingDirectory=/home/you/copilot-matrix-bridge
ExecStart=python3 bridge.py
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now copilot-bridge
```

---

## E2E Encryption

- **Docker**: Full E2E encryption via libolm (included in the image)
- **Standalone on macOS/Linux**: Install `libolm-dev` and `pip install python-olm` for E2E support
- **Standalone on Windows**: E2E encryption is **not supported** (python-olm doesn't build). Create rooms with encryption disabled, or use Docker.

The bridge auto-detects whether E2E is available and falls back gracefully.

---

## Project Structure

```
copilot-matrix-bridge/
├── bridge.py              # Entry point — wires everything together
├── matrix_client.py       # Matrix connection, message handling, E2E
├── copilot_runner.py      # Spawns copilot CLI, streams JSONL events
├── commands.py            # :command handlers
├── room_store.py          # SQLite: room → project + session mapping
├── project_discovery.py   # Scans for git repos under projects_root
├── config.yaml.example    # Device config template
├── .env.example           # Matrix credentials template
├── requirements.txt       # Python dependencies
├── Dockerfile             # Docker image with libolm + Node.js
└── docker-compose.yml     # Docker Compose setup
```

## License

MIT
