"""Matrix client: connect, sync, handle invites and messages."""

import asyncio
import logging
from typing import Callable, Awaitable, Optional

from nio import (
    AsyncClient,
    InviteMemberEvent,
    MatrixRoom,
    MegolmEvent,
    RoomMessageText,
    LoginResponse,
    LoginError,
)

logger = logging.getLogger(__name__)


class MatrixBridge:
    def __init__(
        self,
        homeserver: str,
        bot_user: str,
        bot_password: str,
        owner_id: str,
        device_name: str,
        on_message: Optional[Callable[[str, str], Awaitable[str]]] = None,
    ):
        self.homeserver = homeserver
        self.bot_user = bot_user
        self.bot_password = bot_password
        self.owner_id = owner_id
        self.device_name = device_name
        self.on_message = on_message

        self.client = AsyncClient(homeserver, bot_user)
        self._initial_sync_done = False

    async def start(self):
        """Login and start the sync loop."""
        logger.info("Logging in as %s to %s", self.bot_user, self.homeserver)

        resp = await self.client.login(
            self.bot_password, device_name=f"copilot-bridge-{self.device_name}"
        )

        if isinstance(resp, LoginError):
            logger.error("Login failed: %s", resp.message)
            raise RuntimeError(f"Matrix login failed: {resp.message}")

        logger.info("Logged in. Device ID: %s", resp.device_id)

        # Register event callbacks
        self.client.add_event_callback(self._on_room_message, RoomMessageText)
        self.client.add_event_callback(self._on_invite, InviteMemberEvent)
        self.client.add_event_callback(self._on_encrypted_message, MegolmEvent)

        # Initial sync to catch up — we ignore messages from before we started
        logger.info("Performing initial sync...")
        await self.client.sync(timeout=10000)
        self._initial_sync_done = True
        logger.info("Initial sync complete. Listening for messages...")

        # Continuous sync
        await self.client.sync_forever(timeout=30000, full_state=True)

    async def stop(self):
        """Gracefully disconnect."""
        logger.info("Shutting down Matrix client...")
        await self.client.close()

    async def send_message(self, room_id: str, message: str):
        """Send a text message (markdown) to a room."""
        await self.client.room_send(
            room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": message,
                "format": "org.matrix.custom.html",
                "formatted_body": self._md_to_html(message),
            },
        )

    async def send_typing(self, room_id: str, typing: bool = True, timeout: int = 30000):
        """Show typing indicator."""
        await self.client.room_typing(room_id, typing, timeout=timeout)

    async def _on_invite(self, room: MatrixRoom, event: InviteMemberEvent):
        """Auto-accept invites from the owner."""
        if event.state_key != self.client.user_id:
            return

        if event.sender == self.owner_id:
            logger.info("Accepting invite to %s from %s", room.room_id, event.sender)
            await self.client.join(room.room_id)
            await self.send_message(
                room.room_id,
                f"🤖 **Connected to {self.device_name}**\n\n"
                "Use `/projects` to see available projects, "
                "or `/project <name>` to set one.\n"
                "Type `/help` for all commands.",
            )
        else:
            logger.warning("Ignoring invite from non-owner: %s", event.sender)

    async def _on_room_message(self, room: MatrixRoom, event: RoomMessageText):
        """Handle incoming messages."""
        # Skip messages from before we started
        if not self._initial_sync_done:
            return

        # Ignore our own messages
        if event.sender == self.client.user_id:
            return

        # Only respond to the owner
        if event.sender != self.owner_id:
            logger.debug("Ignoring message from non-owner: %s", event.sender)
            return

        message = event.body.strip()
        if not message:
            return

        logger.info("Message in %s from %s: %s", room.display_name, event.sender, message[:80])

        if self.on_message:
            try:
                await self.send_typing(room.room_id, True)
                response = await self.on_message(room.room_id, message)
                await self.send_typing(room.room_id, False)

                if response:
                    # Chunk long responses (Matrix handles long messages, but
                    # very long ones render poorly in Element)
                    for chunk in self._chunk_message(response):
                        await self.send_message(room.room_id, chunk)

            except Exception as e:
                logger.exception("Error handling message")
                await self.send_typing(room.room_id, False)
                await self.send_message(room.room_id, f"❌ Bridge error: {e}")

    async def _on_encrypted_message(self, room: MatrixRoom, event: MegolmEvent):
        """Handle encrypted messages we can't decrypt."""
        if not self._initial_sync_done:
            return
        if event.sender == self.client.user_id:
            return

        # Only warn once per room
        room_id = room.room_id
        if not hasattr(self, "_encryption_warned"):
            self._encryption_warned = set()

        if room_id not in self._encryption_warned:
            self._encryption_warned.add(room_id)
            logger.warning("Encrypted message in %s — cannot decrypt", room.display_name)
            await self.send_message(
                room_id,
                "🔒 **I can't read encrypted messages.**\n\n"
                "Please create rooms with encryption **disabled**:\n"
                "  1. Create room → Show advanced → **turn off** \"Enable encryption\"\n"
                "  2. Or in Room Settings → Security → Encryption must be off\n\n"
                "This is required because the bridge runs without E2E key management.",
            )

    @staticmethod
    def _chunk_message(text: str, max_len: int = 16000) -> list[str]:
        """Split long messages into chunks, breaking at line boundaries."""
        if len(text) <= max_len:
            return [text]

        chunks = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > max_len:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line

        if current:
            chunks.append(current)

        return chunks

    @staticmethod
    def _md_to_html(text: str) -> str:
        """Basic markdown pass-through. Matrix clients handle rendering."""
        # Matrix clients like Element render markdown natively from the body.
        # The formatted_body is a fallback — we just pass through for now.
        return text.replace("\n", "<br>")
