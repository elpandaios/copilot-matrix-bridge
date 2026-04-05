"""Spawn copilot CLI with streaming JSONL output and interactive ask_user support."""

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable, Awaitable

import yaml

logger = logging.getLogger(__name__)

# Callback types for streaming events to Matrix
OnStepCallback = Callable[[str, str], Awaitable[None]]  # (room_id, step_text)
OnAskUserCallback = Callable[[str, str, list[str]], Awaitable[str]]  # (room_id, question, choices) -> answer


@dataclass
class CopilotResult:
    output: str
    exit_code: int = 0
    timed_out: bool = False


class CopilotRunner:
    def __init__(self, copilot_command: str = "copilot", timeout: int = 10800):
        self.copilot_command = copilot_command
        self.timeout = timeout
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}

    async def run(
        self,
        message: str,
        session_id: str,
        room_id: str,
        cwd: Optional[str] = None,
        mode: str = "chat",
        on_step: Optional[OnStepCallback] = None,
        on_ask_user: Optional[OnAskUserCallback] = None,
    ) -> CopilotResult:
        """Run copilot interactively, streaming events to Matrix."""
        prompt, extra_flags = self._build_prompt_and_flags(message, mode)
        args = self._build_args(prompt, session_id, extra_flags)
        logger.info("Running copilot session=%s mode=%s cwd=%s", session_id, mode, cwd)

        try:
            process = await asyncio.create_subprocess_exec(
                self.copilot_command, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            self._active_processes[session_id] = process

            try:
                final_message = await asyncio.wait_for(
                    self._stream_events(process, room_id, on_step, on_ask_user),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                self._active_processes.pop(session_id, None)
                return CopilotResult(
                    output="Copilot timed out. Session preserved, send another message to continue.",
                    exit_code=-1, timed_out=True,
                )

            await process.wait()
            self._active_processes.pop(session_id, None)
            return CopilotResult(
                output=final_message or "Done (no output).",
                exit_code=process.returncode or 0,
            )

        except FileNotFoundError:
            return CopilotResult(
                output="Copilot CLI not found. Is `{}` on PATH?".format(self.copilot_command),
                exit_code=-1,
            )
        except Exception as e:
            logger.exception("Copilot execution failed")
            return CopilotResult(output=f"Error: {e}", exit_code=-1)

    async def _stream_events(self, process, room_id, on_step, on_ask_user) -> str:
        """Read JSONL events from copilot stdout, stream steps to Matrix in real-time."""
        final_message = ""

        while True:
            line = await process.stdout.readline()
            if not line:
                break
            line = line.decode("utf-8", errors="replace").strip()
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

                # Handle ask_user specially
                if tool_name == "ask_user" and on_ask_user:
                    question = tool_input.get("question", "")
                    choices = tool_input.get("choices", [])
                    try:
                        answer = await on_ask_user(room_id, question, choices)
                        if process.stdin:
                            process.stdin.write((answer + "\n").encode("utf-8"))
                            await process.stdin.drain()
                    except Exception:
                        logger.warning("Failed to handle ask_user", exc_info=True)
                elif on_step:
                    step = self._format_tool_start(tool_name, tool_input)
                    if step:
                        try:
                            await on_step(room_id, step)
                        except Exception:
                            logger.debug("Failed to send step", exc_info=True)

            elif event_type == "tool.execution_complete" and on_step:
                tool_name = data.get("toolName", "")
                result_data = data.get("result", {})
                output = result_data.get("content", "") if isinstance(result_data, dict) else ""
                step = self._format_tool_end(tool_name, output)
                if step:
                    try:
                        await on_step(room_id, step)
                    except Exception:
                        logger.debug("Failed to send tool result", exc_info=True)

            elif event_type == "assistant.message":
                content = data.get("content", "").strip()
                if content:
                    final_message = content

        return final_message

    async def kill_all(self):
        count = len(self._active_processes)
        if count == 0:
            return 0
        logger.info("Killing %d active copilot process(es)...", count)
        for sid, proc in list(self._active_processes.items()):
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        self._active_processes.clear()
        return count

    @property
    def active_count(self) -> int:
        finished = [s for s, p in self._active_processes.items() if p.returncode is not None]
        for s in finished:
            self._active_processes.pop(s, None)
        return len(self._active_processes)

    @staticmethod
    def get_session_info(session_id: str) -> dict:
        """Read workspace.yaml for a copilot session."""
        ws_path = Path.home() / ".copilot" / "session-state" / session_id / "workspace.yaml"
        if not ws_path.exists():
            return {}
        try:
            with open(ws_path, "r") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}

    @staticmethod
    def get_git_branch(cwd: str) -> Optional[str]:
        """Get the current git branch for a project directory."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=cwd, capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    @staticmethod
    def list_sessions(projects_root: str = "") -> list[dict]:
        """List all copilot sessions with their metadata."""
        state_dir = Path.home() / ".copilot" / "session-state"
        if not state_dir.exists():
            return []
        sessions = []
        for d in sorted(state_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            ws = d / "workspace.yaml"
            if not ws.exists():
                continue
            try:
                with open(ws, "r") as f:
                    data = yaml.safe_load(f) or {}
                # Optionally filter by projects_root
                cwd = data.get("cwd", "")
                if projects_root and not cwd.replace("\\", "/").startswith(projects_root.replace("\\", "/")):
                    continue
                sessions.append(data)
            except Exception:
                continue
        return sessions

    def _format_tool_start(self, tool_name: str, tool_input: dict) -> Optional[str]:
        if tool_name == "ask_user":
            return None
        if tool_name == "report_intent":
            intent = tool_input.get("intent", "")
            return f"💭 _{intent}_" if intent else None
        if tool_name in ("shell", "powershell"):
            desc = tool_input.get("description", "")
            cmd = tool_input.get("command", "")
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
        elif tool_name in ("web_search", "web_fetch"):
            query = tool_input.get("query", tool_input.get("url", ""))
            return f"🌐 Searching: {query[:60]}" if query else None
        else:
            desc = tool_input.get("description", "")
            return f"⚡ {tool_name}: {desc}" if desc else f"⚡ {tool_name}"

    def _format_tool_end(self, tool_name: str, output: str) -> Optional[str]:
        if tool_name in ("shell", "powershell") and output:
            clean = output.strip()
            idx = clean.rfind("<exited")
            if idx > 0:
                clean = clean[:idx].strip()
            if clean and len(clean) < 200:
                return f"  └ `{clean}`"
        return None

    @staticmethod
    def _short_path(path: str) -> str:
        parts = path.replace("\\", "/").rstrip("/").split("/")
        return "/".join(parts[-2:]) if len(parts) > 2 else path

    def _build_prompt_and_flags(self, message, mode):
        extra_flags = []
        if mode == "plan":
            return f"[[PLAN]] {message}", extra_flags
        elif mode == "auto":
            extra_flags = ["--autopilot"]
            return message, extra_flags
        return message, extra_flags

    def _build_args(self, prompt, session_id, extra_flags):
        return [
            f"--resume={session_id}",
            "-p", prompt,
            "--yolo",
            "--output-format", "json",
            *extra_flags,
        ]
