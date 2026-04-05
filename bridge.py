"""Copilot Matrix Bridge — main entry point."""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from commands import CommandHandler, parse_prefix
from copilot_runner import CopilotRunner
from matrix_client import MatrixBridge
from project_discovery import ProjectDiscovery
from room_store import RoomStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bridge")


def load_config() -> dict:
    """Load config.yaml from the script directory."""
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        logger.error("config.yaml not found. Copy config.yaml.example → config.yaml")
        sys.exit(1)

    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def main():
    load_dotenv()
    config = load_config()

    # Required env vars
    homeserver = os.environ.get("MATRIX_HOMESERVER")
    bot_user = os.environ.get("MATRIX_BOT_USER")
    bot_password = os.environ.get("MATRIX_BOT_PASSWORD")
    owner_id = os.environ.get("MATRIX_OWNER_ID")

    missing = []
    for name, val in [
        ("MATRIX_HOMESERVER", homeserver),
        ("MATRIX_BOT_USER", bot_user),
        ("MATRIX_BOT_PASSWORD", bot_password),
        ("MATRIX_OWNER_ID", owner_id),
    ]:
        if not val:
            missing.append(name)
    if missing:
        logger.error("Missing env vars: %s. See .env.example", ", ".join(missing))
        sys.exit(1)

    device_name = config.get("device_name", "Unknown Device")
    projects_root = config.get("projects_root", ".")
    copilot_command = config.get("copilot_command", "copilot")
    copilot_timeout = config.get("copilot_timeout", 300)

    # Init components
    db_path = str(Path(__file__).parent / "bridge.db")
    room_store = RoomStore(db_path=db_path)
    project_discovery = ProjectDiscovery(projects_root)
    copilot_runner = CopilotRunner(
        copilot_command=copilot_command, timeout=copilot_timeout
    )
    command_handler = CommandHandler(
        room_store=room_store,
        project_discovery=project_discovery,
        device_name=device_name,
    )

    async def on_message(room_id: str, message: str) -> str:
        """Route a message to commands or copilot."""
        # Check for slash commands first
        cmd_result = command_handler.handle(room_id, message)
        if cmd_result.handled:
            return cmd_result.response

        # Check for inline prefix override
        mode_override, clean_message = parse_prefix(message)

        # Ensure we have a session for this room
        state = room_store.ensure_session(room_id)

        if not state.project_path:
            return (
                "⚠️ No project set for this room.\n"
                "Use `/project <name>` to set one, or `/projects` to see what's available."
            )

        # Determine effective mode
        effective_mode = mode_override or state.mode

        # Run copilot
        result = await copilot_runner.run(
            message=clean_message,
            session_id=state.session_id,
            cwd=state.project_path,
            mode=effective_mode,
        )

        return result.output

    # Create Matrix bridge
    bridge = MatrixBridge(
        homeserver=homeserver,
        bot_user=bot_user,
        bot_password=bot_password,
        owner_id=owner_id,
        device_name=device_name,
        on_message=on_message,
    )

    # Run
    logger.info("🤖 Copilot Matrix Bridge starting...")
    logger.info("   Device: %s", device_name)
    logger.info("   Projects: %s", projects_root)
    logger.info("   Homeserver: %s", homeserver)
    logger.info("   Bot user: %s", bot_user)
    logger.info(
        "   Available projects: %s", ", ".join(project_discovery.list_projects()[:10])
    )

    loop = asyncio.new_event_loop()

    # Graceful shutdown
    def shutdown_handler():
        logger.info("Shutdown signal received...")
        loop.create_task(bridge.stop())

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, shutdown_handler)
        loop.add_signal_handler(signal.SIGINT, shutdown_handler)

    try:
        loop.run_until_complete(bridge.start())
    except KeyboardInterrupt:
        logger.info("Interrupted. Shutting down...")
        loop.run_until_complete(bridge.stop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
