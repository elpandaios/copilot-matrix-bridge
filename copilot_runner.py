"""Spawn copilot CLI one-shot per message with --resume for session persistence."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CopilotResult:
    output: str
    steps: list[str] = field(default_factory=list)
    exit_code: int = 0
    timed_out: bool = False

    def format_full(self) -> str:
        """Format output with thinking steps for Matrix."""
        parts = []
        if self.steps:
            for step in self.steps:
                parts.append(step)
            parts.append("")  # blank line before response
        if self.output:
            parts.append(self.output)
        return "\n".join(parts) if parts else "✅ Done (no output)."


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

            raw = stdout.decode("utf-8", errors="replace").strip()
            if not raw and stderr:
                raw = "⚠️ " + stderr.decode("utf-8", errors="replace").strip()

            result = self._parse_json_output(raw)
            result.exit_code = process.returncode or 0
            return result

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

    def _parse_json_output(self, raw: str) -> CopilotResult:
        """Parse JSONL output from copilot into structured result."""
        steps = []
        final_message = ""
        tool_results = []

        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")
            data = event.get("data", {})

            if event_type == "tool.execution_start":
                tool_name = data.get("toolName", "")
                tool_input = data.get("arguments", {})
                step = self._format_tool_start(tool_name, tool_input)
                if step:
                    steps.append(step)

            elif event_type == "tool.execution_complete":
                tool_name = data.get("toolName", "")
                result_data = data.get("result", {})
                output = result_data.get("content", "") if isinstance(result_data, dict) else str(result_data)
                step = self._format_tool_end(tool_name, output)
                if step:
                    tool_results.append(step)

            elif event_type == "assistant.message":
                content = data.get("content", "").strip()
                if content:
                    final_message = content

        if not final_message and not steps:
            # Fallback: raw wasn't JSON, just return it as-is
            return CopilotResult(output=raw or "✅ Done (no output).")

        return CopilotResult(output=final_message, steps=steps + tool_results)

    def _format_tool_start(self, tool_name: str, tool_input: dict) -> Optional[str]:
        """Format a tool invocation step for display."""
        # Skip internal tools
        if tool_name in ("report_intent",):
            return None

        if tool_name in ("shell", "powershell"):
            cmd = tool_input.get("command", "")
            desc = tool_input.get("description", "")
            label = desc or (cmd[:60] + "..." if len(cmd) > 60 else cmd)
            return f"🔧 `{label}`" if label else None
        elif tool_name in ("read", "view"):
            path = tool_input.get("filePath", tool_input.get("path", ""))
            return f"📄 Reading `{self._short_path(path)}`" if path else None
        elif tool_name == "edit":
            path = tool_input.get("filePath", tool_input.get("path", ""))
            return f"✏️ Editing `{self._short_path(path)}`" if path else None
        elif tool_name == "create":
            path = tool_input.get("filePath", tool_input.get("path", ""))
            return f"📝 Creating `{self._short_path(path)}`" if path else None
        elif tool_name == "glob":
            pattern = tool_input.get("pattern", "")
            return f"🔍 Finding `{pattern}`" if pattern else None
        elif tool_name == "grep":
            pattern = tool_input.get("pattern", "")
            return f"🔍 Searching `{pattern}`" if pattern else None
        elif tool_name in ("list_dir", "list"):
            path = tool_input.get("path", ".")
            return f"📂 Listing `{self._short_path(path)}`"
        elif tool_name.startswith("github-mcp"):
            method = tool_input.get("method", tool_name)
            return f"🐙 GitHub: {method}"
        elif tool_name == "task":
            desc = tool_input.get("description", "sub-agent")
            return f"🤖 Delegating: {desc}"
        elif tool_name == "web_search" or tool_name == "web_fetch":
            query = tool_input.get("query", tool_input.get("url", ""))
            return f"🌐 Searching: {query[:60]}" if query else None
        else:
            desc = tool_input.get("description", "")
            return f"⚡ {tool_name}: {desc}" if desc else f"⚡ {tool_name}"

    def _format_tool_end(self, tool_name: str, output: str) -> Optional[str]:
        """Format tool result for display (only for notable results)."""
        if tool_name in ("shell", "powershell") and output:
            # Strip exit code suffix
            clean = output.strip()
            if clean.endswith(">"):
                # Remove "<exited with exit code X>"
                idx = clean.rfind("<exited")
                if idx > 0:
                    clean = clean[:idx].strip()
            lines = clean.split("\n")
            if lines and len(clean) < 200:
                return f"  └ `{clean}`"
        return None

    @staticmethod
    def _short_path(path: str) -> str:
        """Shorten a path for display — just filename or last 2 components."""
        parts = path.replace("\\", "/").rstrip("/").split("/")
        if len(parts) <= 2:
            return path
        return "/".join(parts[-2:])

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
            "--yolo",
            "--output-format",
            "json",
            *extra_flags,
        ]
