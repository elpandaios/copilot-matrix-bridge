"""Handle slash commands from Matrix messages."""

import logging
from typing import Optional

from room_store import RoomStore
from project_discovery import ProjectDiscovery
from copilot_runner import CopilotRunner

logger = logging.getLogger(__name__)

VALID_MODES = {"chat", "plan", "auto"}


class CommandResult:
    def __init__(self, response: str, handled: bool = True):
        self.response = response
        self.handled = handled  # If False, message should be forwarded to copilot


class CommandHandler:
    def __init__(
        self,
        room_store: RoomStore,
        project_discovery: ProjectDiscovery,
        copilot_runner: CopilotRunner,
        device_name: str,
    ):
        self.room_store = room_store
        self.project_discovery = project_discovery
        self.copilot_runner = copilot_runner
        self.device_name = device_name

    def handle(self, room_id: str, message: str) -> CommandResult:
        """Process a message. Returns CommandResult with handled=False if not a command."""
        stripped = message.strip()

        if not stripped.startswith("/"):
            return CommandResult("", handled=False)

        parts = stripped.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        handlers = {
            "/project": self._cmd_project,
            "/projects": self._cmd_projects,
            "/mode": self._cmd_mode,
            "/status": self._cmd_status,
            "/reset": self._cmd_reset,
            "/shutdown": self._cmd_shutdown,
            "/help": self._cmd_help,
        }

        handler = handlers.get(command)
        if handler:
            return handler(room_id, arg)

        return CommandResult("", handled=False)

    def _cmd_project(self, room_id: str, arg: str) -> CommandResult:
        if not arg:
            return CommandResult(
                "Usage: `/project <name>`\nUse `/projects` to see available projects."
            )

        path = self.project_discovery.resolve(arg)
        if not path:
            projects = self.project_discovery.list_projects()
            suggestion = "\n".join(f"  • {p}" for p in projects[:15])
            return CommandResult(
                f"❌ Project `{arg}` not found.\n\nAvailable projects:\n{suggestion}"
            )

        state = self.room_store.set_project(room_id, path)
        return CommandResult(
            f"✅ Working in **{arg}**\n`{path}`\n\nNew copilot session: `{state.session_id[:8]}...`"
        )

    def _cmd_projects(self, room_id: str, arg: str) -> CommandResult:
        projects = self.project_discovery.list_projects()
        if not projects:
            return CommandResult("No projects found.")

        state = self.room_store.get(room_id)
        lines = []
        for p in projects:
            current = self.project_discovery.resolve(p)
            marker = " ← current" if current == state.project_path else ""
            lines.append(f"  • `{p}`{marker}")

        return CommandResult(
            f"📂 **Available projects** ({len(projects)}):\n" + "\n".join(lines)
        )

    def _cmd_mode(self, room_id: str, arg: str) -> CommandResult:
        if not arg or arg.lower() not in VALID_MODES:
            state = self.room_store.get(room_id)
            return CommandResult(
                f"Current mode: **{state.mode}**\n\n"
                "Usage: `/mode <chat|plan|auto>`\n"
                "  • `chat` — conversational, answers and stops\n"
                "  • `plan` — structured planning, no execution\n"
                "  • `auto` — full send, keeps working until done\n\n"
                "You can also use prefixes: `plan: msg` or `do: msg`"
            )

        mode = arg.lower()
        self.room_store.set_mode(room_id, mode)
        mode_desc = {
            "chat": "💬 Conversational — answers and stops",
            "plan": "📋 Planning — structured plans, no execution",
            "auto": "🚀 Autopilot — keeps working until done",
        }
        return CommandResult(f"Mode set to **{mode}**\n{mode_desc[mode]}")

    def _cmd_status(self, room_id: str, arg: str) -> CommandResult:
        state = self.room_store.get(room_id)
        project = state.project_path or "Not set (use `/project <name>`)"
        session = f"`{state.session_id[:8]}...`" if state.session_id else "None"
        active = self.copilot_runner.active_count

        return CommandResult(
            f"🤖 **Status**\n"
            f"  • Device: {self.device_name}\n"
            f"  • Project: {project}\n"
            f"  • Mode: {state.mode}\n"
            f"  • Session: {session}\n"
            f"  • Active copilot processes: {active}"
        )

    def _cmd_reset(self, room_id: str, arg: str) -> CommandResult:
        self.room_store.reset_session(room_id)
        return CommandResult(
            "🔄 Session reset. Next message will start a fresh copilot session."
        )

    def _cmd_shutdown(self, room_id: str, arg: str) -> CommandResult:
        active = self.copilot_runner.active_count
        if active == 0:
            return CommandResult("No copilot processes running.")
        # kill_all is async, but we return a message now — the actual kill
        # is best done from the async context. We'll mark it as needing async.
        self._pending_shutdown = True
        return CommandResult(
            f"🛑 Killing {active} active copilot process(es)..."
        )

    def _cmd_help(self, room_id: str, arg: str) -> CommandResult:
        return CommandResult(
            "🤖 **Copilot Matrix Bridge**\n\n"
            "**Commands:**\n"
            "  `/project <name>` — set working directory\n"
            "  `/projects` — list available projects\n"
            "  `/mode <chat|plan|auto>` — set room mode\n"
            "  `/status` — show current state\n"
            "  `/reset` — start fresh copilot session\n"
            "  `/shutdown` — kill stuck copilot processes\n"
            "  `/help` — this message\n\n"
            "**Prefixes** (override mode for one message):\n"
            "  `plan: <msg>` — force plan mode\n"
            "  `do: <msg>` — force autopilot mode\n\n"
            "**Modes:**\n"
            "  `chat` — conversational (default)\n"
            "  `plan` — structured planning\n"
            "  `auto` — full autopilot"
        )


def parse_prefix(message: str) -> tuple[Optional[str], str]:
    """Check for inline prefix overrides. Returns (mode_override, clean_message)."""
    lower = message.lstrip().lower()

    if lower.startswith("plan:"):
        return "plan", message.lstrip()[5:].lstrip()
    elif lower.startswith("do:"):
        return "auto", message.lstrip()[3:].lstrip()

    return None, message
