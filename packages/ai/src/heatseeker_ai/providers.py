"""Bounded subprocess adapters for Codex and Claude Code source research."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from heatseeker_common.settings import Settings
from pydantic import BaseModel

from heatseeker_ai.contracts import EntityWebResearchResult, SourceExpansionResult


class ProviderError(RuntimeError):
    """A provider could not complete a valid bounded invocation."""


class ScoutCancelled(ProviderError):
    """The owning HeatSeeker job requested cooperative cancellation."""


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider: str
    installed: bool
    authenticated: bool | None
    version: str | None = None
    detail: str | None = None
    model_suggestions: tuple[str, ...] = ()
    path: str | None = None  # resolved executable actually used
    hint: str | None = None  # one actionable next step for the user, if any


@dataclass(frozen=True, slots=True)
class ProviderResult:
    output: BaseModel
    raw_output: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    metadata: dict = field(default_factory=dict)


class SourceScoutProvider(Protocol):
    name: str

    def complete(
        self,
        prompt: str,
        *,
        model: str | None,
        budgets: dict,
        cancelled: Callable[[], bool],
    ) -> ProviderResult: ...


def _discovery_dirs() -> list[Path]:
    """Well-known per-user CLI install locations, checked when PATH lookup fails.

    Covers the npm/pnpm global folders and the native installers' ~/.local/bin, so a
    CLI that was installed and signed in once for this user keeps working even when
    the process that launched Heatseeker doesn't inherit an updated PATH.
    """
    home = Path.home()
    dirs = [home / ".local" / "bin"]
    if os.name == "nt":
        for env_var, subdir in (("APPDATA", "npm"), ("LOCALAPPDATA", "pnpm")):
            base = os.environ.get(env_var)
            if base:
                dirs.append(Path(base) / subdir)
    else:
        dirs.extend(
            [
                home / ".npm-global" / "bin",
                home / ".volta" / "bin",
                home / ".bun" / "bin",
                Path("/usr/local/bin"),
                Path("/opt/homebrew/bin"),
            ]
        )
    return dirs


# CLI binaries bundled inside IDE extensions. The Claude Code VS Code extension ships
# the full native CLI, and credentials are per-user (shared between bundled and
# standalone installs), so the sign-in done in the IDE works for Heatseeker too.
_IDE_EXTENSION_GLOBS = {
    "claude": ("anthropic.claude-code-*",),
    "codex": ("openai.chatgpt-*",),
}
_IDE_DIRS = (".vscode", ".vscode-insiders", ".cursor", ".windsurf")


def _extension_version_key(directory: Path) -> tuple[int, ...]:
    import re

    match = re.search(r"(\d+(?:\.\d+)+)", directory.name)
    return tuple(int(part) for part in match.group(1).split(".")) if match else (0,)


def _ide_bundled_binaries(command: str) -> list[Path]:
    globs = _IDE_EXTENSION_GLOBS.get(command, ())
    if not globs:
        return []
    exe_name = f"{command}.exe" if os.name == "nt" else command
    found: list[Path] = []
    for ide_dir in _IDE_DIRS:
        extensions_root = Path.home() / ide_dir / "extensions"
        if not extensions_root.is_dir():
            continue
        for pattern in globs:
            for extension in sorted(
                extensions_root.glob(pattern), key=_extension_version_key, reverse=True
            ):
                found.extend(sorted(extension.rglob(exe_name)))
    return found


def _resolve_command(command: str) -> str | None:
    candidate = Path(command).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    found = shutil.which(command)
    if found:
        return found
    if Path(command).name != command:
        return None  # an explicit path that doesn't exist — don't guess elsewhere
    names = [f"{command}.exe", f"{command}.cmd", command] if os.name == "nt" else [command]
    for directory in _discovery_dirs():
        for name in names:
            fallback = directory / name
            if fallback.is_file():
                return str(fallback)
    for bundled in _ide_bundled_binaries(command):
        if bundled.is_file():
            return str(bundled)
    return None


def _run_probe(command: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    output = (completed.stdout or completed.stderr).strip()
    return completed.returncode, output[:1000]


_INSTALL_HINTS = {
    "codex": (
        "Install the Codex CLI (npm i -g @openai/codex), or install the ChatGPT "
        "VS Code extension (its bundled codex.exe is detected automatically), or set "
        "HEATSEEKER_AI_CODEX_COMMAND to a full path. No PATH edit is needed — "
        "npm/pnpm global, ~/.local/bin, and IDE extension folders are searched."
    ),
    "claude": (
        "Install the Claude Code CLI (npm i -g @anthropic-ai/claude-code), or install "
        "the Claude Code VS Code extension (its bundled claude.exe is detected "
        "automatically), or set HEATSEEKER_AI_CLAUDE_COMMAND to a full path. No PATH "
        "edit is needed — npm/pnpm global, ~/.local/bin, and IDE extension folders "
        "are searched."
    ),
}

_LOGIN_HINTS = {
    "codex": (
        "Run `codex login` once as this Windows user — a browser sign-in tied to your "
        "ChatGPT/Codex subscription; the credential stays in the CLI's own store."
    ),
    "claude": (
        "Run `claude /login` once as this Windows user — a browser sign-in tied to your "
        "Claude subscription; the credential stays in the CLI's own store."
    ),
}


def provider_health(settings: Settings, provider: str) -> ProviderHealth:
    if provider == "disabled":
        return ProviderHealth("disabled", True, True, detail="AI calls are disabled")
    command_name = settings.ai_codex_command if provider == "codex" else settings.ai_claude_command
    command = _resolve_command(command_name)
    if command is None:
        return ProviderHealth(
            provider,
            False,
            False,
            detail=f"{command_name!r} not found on PATH or in known install folders",
            hint=_INSTALL_HINTS.get(provider),
        )
    version_code, version = _run_probe([command, "--version"])
    if provider == "codex":
        auth_code, auth = _run_probe([command, "login", "status"])
        suggestions: tuple[str, ...] = ()
    else:
        auth_code, auth = _run_probe([command, "auth", "status", "--text"])
        suggestions = ("sonnet", "opus", "haiku")
    detail = auth or ("ready" if auth_code == 0 else "not authenticated")
    return ProviderHealth(
        provider,
        version_code == 0,
        auth_code == 0,
        version=version or None,
        detail=detail,
        model_suggestions=suggestions,
        path=command,
        hint=None if auth_code == 0 else _LOGIN_HINTS.get(provider),
    )


def all_provider_health(settings: Settings) -> list[ProviderHealth]:
    return [provider_health(settings, name) for name in ("codex", "claude", "disabled")]


def _terminate_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def _run_agent(
    command: list[str],
    prompt: str,
    *,
    cwd: Path,
    timeout_seconds: float,
    max_output_bytes: int,
    cancelled: Callable[[], bool],
) -> tuple[str, str]:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    if os.name == "nt":
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name != "nt",
        creationflags=creationflags,
    )
    deadline = time.monotonic() + timeout_seconds
    pending_input: str | None = prompt
    while True:
        if cancelled():
            _terminate_tree(process)
            raise ScoutCancelled("source-scout run cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_tree(process)
            raise ProviderError(f"provider exceeded {timeout_seconds:g}s timeout")
        try:
            stdout, stderr = process.communicate(input=pending_input, timeout=min(1.0, remaining))
            break
        except subprocess.TimeoutExpired:
            pending_input = None
    if len(stdout.encode("utf-8")) + len(stderr.encode("utf-8")) > max_output_bytes:
        raise ProviderError("provider output exceeded configured byte limit")
    if process.returncode != 0:
        detail = stderr.strip() or stdout.strip() or f"exit code {process.returncode}"
        if len(detail) > 4000:
            detail = f"{detail[:1000]}\n... provider output truncated ...\n{detail[-2900:]}"
        raise ProviderError(detail)
    return stdout, stderr


def _json_result[T: BaseModel](value: str, result_type: type[T]) -> T:
    try:
        return result_type.model_validate_json(value)
    except Exception as exc:
        raise ProviderError(f"provider returned invalid structured output: {exc}") from exc


def _codex_schema(value):
    """Remove annotation keywords unsupported by Codex structured outputs.

    Pydantic still performs full URL validation when the response is parsed.
    """
    if isinstance(value, dict):
        unsupported_annotations = {
            "default",
            "format",
            "maxItems",
            "maxLength",
            "maximum",
            "minItems",
            "minLength",
            "minimum",
            "pattern",
            "title",
        }
        result = {
            key: _codex_schema(item)
            for key, item in value.items()
            if key not in unsupported_annotations
        }
        properties = result.get("properties")
        if isinstance(properties, dict):
            result["required"] = list(properties)
            result["additionalProperties"] = False
        return result
    if isinstance(value, list):
        return [_codex_schema(item) for item in value]
    return value


class CodexProvider:
    name = "codex"

    def __init__(self, settings: Settings):
        self.settings = settings

    def complete(
        self,
        prompt: str,
        *,
        model: str | None,
        budgets: dict,
        cancelled: Callable[[], bool],
    ) -> ProviderResult:
        return self._complete_typed(
            prompt, SourceExpansionResult, "source-expansion", model, budgets, cancelled
        )

    def research_entity(
        self,
        prompt: str,
        *,
        model: str | None,
        budgets: dict,
        cancelled: Callable[[], bool],
    ) -> ProviderResult:
        return self._complete_typed(
            prompt, EntityWebResearchResult, "entity-research", model, budgets, cancelled
        )

    def _complete_typed[T: BaseModel](
        self,
        prompt: str,
        result_type: type[T],
        schema_name: str,
        model: str | None,
        budgets: dict,
        cancelled: Callable[[], bool],
    ) -> ProviderResult:
        executable = _resolve_command(self.settings.ai_codex_command)
        if executable is None:
            raise ProviderError("Codex CLI is not installed")
        self.settings.ai_work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.settings.ai_work_dir) as temp:
            workdir = Path(temp)
            schema_path = workdir / f"{schema_name}.schema.json"
            output_path = workdir / "result.json"
            schema_path.write_text(
                json.dumps(_codex_schema(result_type.model_json_schema())),
                encoding="utf-8",
            )
            command = [
                executable,
                "--search",
                "--ask-for-approval",
                "never",
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--config",
                'shell_environment_policy.inherit="none"',
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
            ]
            if model:
                command.extend(["--model", model])
            command.append("-")
            stdout, _stderr = _run_agent(
                command,
                prompt,
                cwd=workdir,
                timeout_seconds=min(
                    float(budgets.get("timeout_seconds", self.settings.ai_scout_timeout_seconds)),
                    self.settings.ai_scout_timeout_seconds,
                ),
                max_output_bytes=self.settings.ai_scout_max_output_bytes,
                cancelled=cancelled,
            )
            if not output_path.exists():
                raise ProviderError("Codex did not write its final structured result")
            raw = output_path.read_text(encoding="utf-8")
            return ProviderResult(
                output=_json_result(raw, result_type), raw_output=raw, metadata={"log": stdout}
            )


class ClaudeProvider:
    name = "claude"

    def __init__(self, settings: Settings):
        self.settings = settings

    def complete(
        self,
        prompt: str,
        *,
        model: str | None,
        budgets: dict,
        cancelled: Callable[[], bool],
    ) -> ProviderResult:
        return self._complete_typed(
            prompt, SourceExpansionResult, model=model, budgets=budgets, cancelled=cancelled
        )

    def research_entity(
        self,
        prompt: str,
        *,
        model: str | None,
        budgets: dict,
        cancelled: Callable[[], bool],
    ) -> ProviderResult:
        return self._complete_typed(
            prompt, EntityWebResearchResult, model=model, budgets=budgets, cancelled=cancelled
        )

    def _complete_typed[T: BaseModel](
        self,
        prompt: str,
        result_type: type[T],
        *,
        model: str | None,
        budgets: dict,
        cancelled: Callable[[], bool],
    ) -> ProviderResult:
        executable = _resolve_command(self.settings.ai_claude_command)
        if executable is None:
            raise ProviderError("Claude Code CLI is not installed")
        self.settings.ai_work_dir.mkdir(parents=True, exist_ok=True)
        schema = json.dumps(result_type.model_json_schema(), separators=(",", ":"))
        command = [
            executable,
            "-p",
            "--safe-mode",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--tools",
            "WebSearch,WebFetch",
            "--allowedTools",
            "WebSearch",
            "WebFetch",
            "--output-format",
            "json",
            "--json-schema",
            schema,
            "--max-turns",
            str(max(1, min(int(budgets.get("max_turns", 8)), 30))),
        ]
        if model:
            command.extend(["--model", model])
        max_budget = budgets.get("max_budget_usd")
        if max_budget is not None:
            command.extend(["--max-budget-usd", str(max(0.01, min(float(max_budget), 100.0)))])
        with tempfile.TemporaryDirectory(dir=self.settings.ai_work_dir) as temp:
            stdout, _stderr = _run_agent(
                command,
                prompt,
                cwd=Path(temp),
                timeout_seconds=min(
                    float(budgets.get("timeout_seconds", self.settings.ai_scout_timeout_seconds)),
                    self.settings.ai_scout_timeout_seconds,
                ),
                max_output_bytes=self.settings.ai_scout_max_output_bytes,
                cancelled=cancelled,
            )
        try:
            wrapper = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Claude returned invalid JSON: {exc}") from exc
        structured = wrapper.get("structured_output")
        if structured is None:
            result_value = wrapper.get("result")
            structured = json.loads(result_value) if isinstance(result_value, str) else result_value
        output = result_type.model_validate(structured)
        usage = wrapper.get("usage") or {}
        return ProviderResult(
            output=output,
            raw_output=json.dumps(structured, ensure_ascii=False),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cost_usd=wrapper.get("total_cost_usd"),
            metadata={"session_id": wrapper.get("session_id")},
        )


def get_provider(settings: Settings, provider: str) -> SourceScoutProvider:
    if not settings.ai_enabled:
        raise ProviderError("AI is disabled by HEATSEEKER_AI_ENABLED")
    if provider == "codex":
        return CodexProvider(settings)
    if provider == "claude":
        return ClaudeProvider(settings)
    raise ProviderError(f"provider {provider!r} cannot execute source research")
