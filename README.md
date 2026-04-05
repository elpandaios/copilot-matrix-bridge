# Copilot Matrix Bridge

Chat with GitHub Copilot CLI from Matrix/Element — on your phone, from any device.

Each device runs a local bridge process. Create rooms in Element, invite the bot, pick a project, and start chatting. Copilot sessions persist across messages via `--resume`.

## Architecture

```
Element (phone/desktop/web)
    ↕
Synapse (matrix.plainwise.com)
    ↕
Local Bridge (this script, per device)
    ↕
copilot --resume=<session> -p "msg" -s --yolo
```

## Quick Start

### 1. Create a bot user on Synapse

SSH into your Synapse server and register a user:

```bash
# On the Synapse server
register_new_matrix_user -u copilot-win -p <password> -c /path/to/homeserver.yaml --no-admin

# Or via Admin API (if enabled):
curl -X PUT "https://matrix.plainwise.com/_synapse/admin/v2/users/@copilot-win:plainwise.com" \
  -H "Authorization: Bearer <admin_token>" \
  -H "Content-Type: application/json" \
  -d '{"password": "<password>", "admin": false}'
```

Use different usernames per device: `copilot-win`, `copilot-mac`, etc.

### 2. Clone and configure

```bash
cd ~/PycharmProjects
# (already cloned)
cd copilot-matrix-bridge

# Config
cp config.yaml.example config.yaml
cp .env.example .env

# Edit .env with your bot credentials
# Edit config.yaml with your device name and projects root
```

**`.env`:**
```
MATRIX_HOMESERVER=https://matrix.plainwise.com
MATRIX_BOT_USER=@copilot-win:plainwise.com
MATRIX_BOT_PASSWORD=your-password
MATRIX_OWNER_ID=@hugo:plainwise.com
```

**`config.yaml`:**
```yaml
device_name: "Windows Desktop"
projects_root: "C:\\Users\\hugom\\PycharmProjects"
copilot_command: "copilot"
copilot_timeout: 300
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run

```bash
python bridge.py
```

## Usage

### In Element

1. Create a Space called "🤖 Copilots" (optional, for organization)
2. Create a new room (e.g., "Fix auth bug")
3. Invite `@copilot-win:plainwise.com`
4. Bot joins and greets you
5. Set a project: `/project Call-System-V6`
6. Start chatting!

### Commands

| Command | Description |
|---------|-------------|
| `/project <name>` | Set working directory |
| `/projects` | List available projects |
| `/mode <chat\|plan\|auto>` | Set room mode |
| `/status` | Show current state |
| `/reset` | Start fresh copilot session |
| `/help` | Show all commands |

### Modes

| Mode | Behavior |
|------|----------|
| `chat` (default) | Conversational — copilot answers and stops |
| `plan` | Planning — structured plans, no code execution |
| `auto` | Autopilot — copilot keeps working until done |

### Inline Prefixes

Override the room mode for a single message:

- `plan: design the auth system` → plan mode for this message
- `do: implement the rate limiter` → autopilot for this message
- (no prefix) → uses the room's current mode

### Multiple Chats

Each room is an independent copilot session, even if multiple rooms use the same project:

- Room "Fix auth" → Call-System-V6 → Session A
- Room "Add logging" → Call-System-V6 → Session B
- Room "Refactor pricing" → Portal-Main → Session C

### Multi-Device

Each device has its own bot user:

| Device | Bot User | Bridge |
|--------|----------|--------|
| Windows | `@copilot-win:plainwise.com` | `python bridge.py` on Windows |
| Mac | `@copilot-mac:plainwise.com` | `python bridge.py` on Mac |

Invite the bot for the device you want to work on.

## How It Works

1. You send a message in a Matrix room
2. Bridge picks it up via `/sync`
3. Looks up the room's project path and copilot session ID
4. Runs: `copilot --resume=<session-id> -p "your message" -s --yolo`
5. Copilot processes the message with full session context
6. Response is sent back to the Matrix room

Session state is managed entirely by Copilot CLI (`~/.copilot/session-state/`). The bridge only stores the mapping of room → project + session ID in a local SQLite database (`bridge.db`).

## Running as a Service (Optional)

### Windows (Task Scheduler)

Create a scheduled task that runs at login:
```
Program: python
Arguments: C:\Users\hugom\PycharmProjects\copilot-matrix-bridge\bridge.py
Start in: C:\Users\hugom\PycharmProjects\copilot-matrix-bridge
```

### Mac (launchd)

Create `~/Library/LaunchAgents/com.plainwise.copilot-bridge.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.plainwise.copilot-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>python3</string>
        <string>/Users/hugo/PycharmProjects/copilot-matrix-bridge/bridge.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/hugo/PycharmProjects/copilot-matrix-bridge</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

Then: `launchctl load ~/Library/LaunchAgents/com.plainwise.copilot-bridge.plist`
