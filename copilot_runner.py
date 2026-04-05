"""Spawn copilot CLI one-shot per message with --resume for session persistence."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CopilotResult:
    output: str
    exit_code: int
    timed_out: bool = False


class CopilotRunner:
    def __init__(self, copilot_command: str = "copilot", timeout: int = 300):
        self.copilot_command = copilot_command
        self.timeout = timeout

    async def run(
        self,
        message: str,
        session_id: str,
        cwd: Optional[str] = None,
        mode: str = "chat",
    ) -> CopilotResult:
        """Run copilot with the given message and return the response."""

        prompt, extra_flags = self._build_prompt_and_flags(message, mode)
        args = self._build_args(prompt, session_id, extra_flags)

        logger.info(
            "Running copilot session=%s mode=%s cwd=%s", session_id, mode, cwd
        )
        logger.debug("Args: %s", args)

        try:
            process = await asyncio.create_subprocess_exec(
                self.copilot_command,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return CopilotResult(
                    output="⏱️ Copilot timed out after {}s. The session is preserved — send another message to continue.".format(
                        self.timeout
                    ),
                    exit_code=-1,
                    timed_out=True,
                )

            output = stdout.decode("utf-8", errors="replace").strip()
            if not output and stderr:
                output = "⚠️ " + stderr.decode("utf-8", errors="replace").strip()

            if not output:
                output = "✅ Done (no output)."

            return CopilotResult(output=output, exit_code=process.returncode or 0)

        except FileNotFoundError:
            return CopilotResult(
                output="❌ Copilot CLI not found. Is `{}` on PATH?".format(
                    self.copilot_command
                ),
                exit_code=-1,
            )
        except Exception as e:
            logger.exception("Copilot execution failed")
            return CopilotResult(output=f"❌ Error: {e}", exit_code=-1)

    def _build_prompt_and_flags(
        self, message: str, mode: str
    ) -> tuple[str, list[str]]:
        """Determine the prompt text and extra CLI flags based on mode."""
        extra_flags = []

        if mode == "plan":
            return f"[[PLAN]] {message}", extra_flags
        elif mode == "auto":
            extra_flags = ["--autopilot", "--no-ask-user"]
            return message, extra_flags
        else:  # chat
            return message, extra_flags

    def _build_args(
        self, prompt: str, session_id: str, extra_flags: list[str]
    ) -> list[str]:
        return [
            f"--resume={session_id}",
            "-p",
            prompt,
            "-s",
            "--yolo",
            *extra_flags,
        ]
