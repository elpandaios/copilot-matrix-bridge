"""Copilot Matrix Bridge — main entry point."""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from commands import CommandHandler, parse_prefix, build_room_name
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
        copilot_runner=copilot_runner,
        device_name=device_name,
    )

    async def handle_pending_renames():
        """Process any pending room renames from commands."""
        pending = getattr(command_handler, '_pending_rename', None)
        if pending:
            command_handler._pending_rename = None
            rid, name = pending
            await bridge.set_room_name(rid, name)

    async def update_room_name_from_session(room_id: str, session_id: str):
        """After copilot replies, read workspace.yaml for session summary and update room name."""
        info = copilot_runner.get_session_info(session_id)
        summary = info.get("summary", "")
        if summary:
            branch = info.get("branch", "")
            cwd = info.get("cwd", "")
            project = cwd.replace("\\", "/").rstrip("/").split("/")[-1] if cwd else ""
            created = str(info.get("created_at", ""))[:10]
            await bridge.set_room_name(room_id, build_room_name(summary, project, branch, created))

    async def on_message(room_id: str, message: str) -> str:
        """Route a message to commands or copilot."""
        # Check for slash commands first
        cmd_result = command_handler.handle(room_id, message)
        if cmd_result.handled:
            if getattr(command_handler, '_pending_shutdown', False):
                command_handler._pending_shutdown = False
                killed = await copilot_runner.kill_all()
                return f"🛑 Killed {killed} copilot process(es)."
            pending_clear = getattr(command_handler, '_pending_clear', None)
            if pending_clear:
                command_handler._pending_clear = None
                _, session_id, project_path = pending_clear
                result = await copilot_runner.run(
                    message="/clear",
                    session_id=session_id,
                    room_id=room_id,
                    cwd=project_path,
                    mode="chat",
                )
                return f"🧹 Session cleared. {result.output}"
            # Handle any pending room renames from /project or /resume
            await handle_pending_renames()
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

        effective_mode = mode_override or state.mode

        # Streaming callback: send each step to Matrix as it happens
        async def on_step(rid: str, step_text: str):
            await bridge.send_message(rid, step_text)

        # ask_user callback: forward question to Matrix, wait for reply
        async def on_ask_user(rid: str, question: str, choices: list[str]) -> str:
            if choices:
                formatted = question + "\n\n"
                for i, choice in enumerate(choices, 1):
                    formatted += f"**{i}.** {choice}\n"
                formatted += "\n_Reply with the number or your answer:_"
            else:
                formatted = question + "\n\n_Reply with your answer:_"
            await bridge.send_message(rid, f"❓ {formatted}")
            answer = await bridge.wait_for_reply(rid)
            if choices and answer.strip().isdigit():
                idx = int(answer.strip()) - 1
                if 0 <= idx < len(choices):
                    return choices[idx]
            return answer

        # Run copilot with streaming
        result = await copilot_runner.run(
            message=clean_message,
            session_id=state.session_id,
            room_id=room_id,
            cwd=state.project_path,
            mode=effective_mode,
            on_step=on_step,
            on_ask_user=on_ask_user,
        )

        # After copilot replies, update room name from session summary
        await update_room_name_from_session(room_id, state.session_id)

        return result.output

    # Create Matrix bridge
    crypto_store = str(Path(__file__).parent / "crypto_store")
    bridge = MatrixBridge(
        homeserver=homeserver,
        bot_user=bot_user,
        bot_password=bot_password,
        owner_id=owner_id,
        device_name=device_name,
        store_path=crypto_store,
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

    async def graceful_shutdown():
        """Kill copilot processes and disconnect from Matrix."""
        killed = await copilot_runner.kill_all()
        if killed:
            logger.info("Killed %d copilot process(es)", killed)
        await bridge.stop()

    # Graceful shutdown
    def shutdown_handler():
        logger.info("Shutdown signal received...")
        loop.create_task(graceful_shutdown())

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, shutdown_handler)
        loop.add_signal_handler(signal.SIGINT, shutdown_handler)

    try:
        loop.run_until_complete(bridge.start())
    except KeyboardInterrupt:
        logger.info("Interrupted. Shutting down...")
        loop.run_until_complete(graceful_shutdown())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
