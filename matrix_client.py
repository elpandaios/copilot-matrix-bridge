"""Matrix client: connect, sync, handle invites and messages."""

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable, Awaitable, Optional

import markdown

from nio import (
    AsyncClient,
    InviteMemberEvent,
    KeyVerificationStart,
    KeyVerificationCancel,
    KeyVerificationKey,
    KeyVerificationMac,
    MatrixRoom,
    MegolmEvent,
    RoomMessageText,
    LoginResponse,
    LoginError,
    ToDeviceError,
    LocalProtocolError,
    crypto,
)

logger = logging.getLogger(__name__)

# Check if E2E support is available
try:
    from nio import AsyncClientConfig
    _E2E_AVAILABLE = True
except ImportError:
    _E2E_AVAILABLE = False


def _has_olm() -> bool:
    """Check if python-olm is installed and functional."""
    try:
        import olm  # noqa: F401
        return True
    except ImportError:
        return False


class MatrixBridge:
    def __init__(
        self,
        homeserver: str,
        bot_user: str,
        bot_password: str,
        owner_id: str,
        device_name: str,
        store_path: str = "",
        on_message: Optional[Callable[[str, str], Awaitable[str]]] = None,
    ):
        self.homeserver = homeserver
        self.bot_user = bot_user
        self.bot_password = bot_password
        self.owner_id = owner_id
        self.device_name = device_name
        self.on_message = on_message
        self._initial_sync_done = False
        self._encryption_warned: set[str] = set()
        # Per-room futures for wait_for_reply (ask_user support)
        self._reply_waiters: dict[str, asyncio.Future] = {}

        self.e2e_enabled = _has_olm()

        if self.e2e_enabled:
            # Set up crypto store for E2E key persistence
            if not store_path:
                store_path = str(Path(__file__).parent / "crypto_store")
            os.makedirs(store_path, exist_ok=True)

            config = AsyncClientConfig(
                store_sync_tokens=True,
                encryption_enabled=True,
            )
            self.client = AsyncClient(
                homeserver, bot_user, store_path=store_path, config=config
            )
            logger.info("E2E encryption enabled (python-olm found)")
        else:
            self.client = AsyncClient(homeserver, bot_user)
            logger.warning("E2E encryption NOT available (python-olm not installed)")

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

        if self.e2e_enabled:
            # Trust the owner's devices automatically
            logger.info("E2E enabled — will auto-trust %s's devices", self.owner_id)

        # Register event callbacks
        self.client.add_event_callback(self._on_room_message, RoomMessageText)
        self.client.add_event_callback(self._on_invite, InviteMemberEvent)
        self.client.add_event_callback(self._on_encrypted_message, MegolmEvent)

        if self.e2e_enabled:
            self.client.add_to_device_callback(
                self._on_key_verification, KeyVerificationStart
            )
            self.client.add_to_device_callback(
                self._on_key_verification_cancel, KeyVerificationCancel
            )
            self.client.add_to_device_callback(
                self._on_key_verification_key, KeyVerificationKey
            )
            self.client.add_to_device_callback(
                self._on_key_verification_mac, KeyVerificationMac
            )

        # Initial sync to catch up — we ignore messages from before we started
        logger.info("Performing initial sync...")
        await self.client.sync(timeout=10000, full_state=True)
        self._initial_sync_done = True

        if self.e2e_enabled:
            await self._trust_owner_devices()

        logger.info("Initial sync complete. Listening for messages...")

        # Continuous sync
        await self.client.sync_forever(timeout=30000, full_state=True)

    async def stop(self):
        """Gracefully disconnect."""
        logger.info("Shutting down Matrix client...")
        await self.client.close()

    async def send_message(self, room_id: str, message: str):
        """Send a text message (markdown) to a room. Auto-encrypts if room has E2E."""
        content = {
            "msgtype": "m.text",
            "body": message,
            "format": "org.matrix.custom.html",
            "formatted_body": self._md_to_html(message),
        }

        try:
            await self.client.room_send(
                room_id,
                message_type="m.room.message",
                content=content,
            )
        except LocalProtocolError as e:
            # If encryption fails, log and try to share keys
            logger.warning("Send failed (likely missing keys): %s", e)
            if self.e2e_enabled:
                await self._share_keys_for_room(room_id)
                await self.client.room_send(
                    room_id,
                    message_type="m.room.message",
                    content=content,
                )

    async def send_typing(self, room_id: str, typing: bool = True, timeout: int = 30000):
        """Show typing indicator."""
        await self.client.room_typing(room_id, typing, timeout=timeout)

    async def set_room_name(self, room_id: str, name: str):
        """Set the display name of a Matrix room."""
        try:
            await self.client.room_put_state(
                room_id, "m.room.name", {"name": name}
            )
            logger.info("Room %s renamed to: %s", room_id, name)
        except Exception as e:
            logger.warning("Failed to rename room %s: %s", room_id, e)

    async def wait_for_reply(self, room_id: str, timeout: float = 300) -> str:
        """Wait for the owner's next message in a specific room (for ask_user)."""
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._reply_waiters[room_id] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return "(no response)"
        finally:
            self._reply_waiters.pop(room_id, None)

    async def _trust_owner_devices(self):
        """Auto-trust all devices belonging to the owner."""
        try:
            devices = self.client.device_store
            if not devices:
                return

            owner_devices = devices.get(self.owner_id, {})
            for device_id, olm_device in owner_devices.items():
                if not self.client.is_device_verified(olm_device):
                    self.client.verify_device(olm_device)
                    logger.info("Auto-trusted device %s for %s", device_id, self.owner_id)
        except Exception as e:
            logger.warning("Could not auto-trust owner devices: %s", e)

    async def _share_keys_for_room(self, room_id: str):
        """Share encryption keys with room members."""
        try:
            room = self.client.rooms.get(room_id)
            if room and room.encrypted:
                resp = await self.client.share_group_session(room_id)
                if isinstance(resp, ToDeviceError):
                    logger.warning("Failed to share group session: %s", resp)
        except Exception as e:
            logger.warning("Key sharing failed: %s", e)

    async def _on_key_verification(self, event: KeyVerificationStart):
        """Accept incoming key verification requests."""
        logger.info("Key verification request from %s", event.sender)
        try:
            await self.client.accept_key_verification(event.transaction_id)
        except Exception as e:
            logger.warning("Failed to accept verification: %s", e)

    async def _on_key_verification_cancel(self, event: KeyVerificationCancel):
        """Handle verification cancellation."""
        logger.info("Key verification cancelled: %s", event.reason)

    async def _on_key_verification_key(self, event: KeyVerificationKey):
        """Handle key exchange step — auto-confirm."""
        logger.info("Key verification key exchange")
        try:
            await self.client.confirm_short_auth_string(event.transaction_id)
        except Exception as e:
            logger.warning("Failed to confirm verification: %s", e)

    async def _on_key_verification_mac(self, event: KeyVerificationMac):
        """Handle MAC verification step."""
        logger.info("Key verification MAC received — verification complete")

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
        if not self._initial_sync_done:
            return

        if event.sender == self.client.user_id:
            return

        if event.sender != self.owner_id:
            logger.debug("Ignoring message from non-owner: %s", event.sender)
            return

        message = event.body.strip()
        if not message:
            return

        logger.info("Message in %s from %s: %s", room.display_name, event.sender, message[:80])

        # If there's a pending ask_user waiter for this room, resolve it instead
        waiter = self._reply_waiters.get(room.room_id)
        if waiter and not waiter.done():
            waiter.set_result(message)
            return

        if self.on_message:
            try:
                await self.send_typing(room.room_id, True)
                response = await self.on_message(room.room_id, message)
                await self.send_typing(room.room_id, False)

                if response:
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

        room_id = room.room_id
        if room_id not in self._encryption_warned:
            self._encryption_warned.add(room_id)

            if self.e2e_enabled:
                logger.warning(
                    "Failed to decrypt in %s — may need device verification",
                    room.display_name,
                )
                await self.send_message(
                    room_id,
                    "🔒 **Couldn't decrypt this message.**\n\n"
                    "Try verifying my device in Element:\n"
                    "  Room → Members → copilot-bot → Verify\n\n"
                    "I'll auto-trust your devices after that.",
                )
                # Attempt to re-trust after this event
                await self._trust_owner_devices()
            else:
                logger.warning("Encrypted message in %s — no E2E support", room.display_name)
                await self.send_message(
                    room_id,
                    "🔒 **I can't read encrypted messages.**\n\n"
                    "Please create rooms with encryption **disabled**:\n"
                    "  Create room → Show advanced → **turn off** \"Enable encryption\"\n\n"
                    "Or run the bridge via Docker for E2E support.",
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
        """Convert markdown to HTML for Matrix formatted_body."""
        return markdown.markdown(
            text,
            extensions=["tables", "fenced_code", "nl2br"],
        )
