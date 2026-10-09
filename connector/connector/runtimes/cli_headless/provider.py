from __future__ import annotations

import os
import shutil
import sys
from typing import Any

from connector.runtime_protocol import (
    RuntimeConfig,
    RuntimeConfigSchema,
    RuntimeProvider,
    RuntimeTypeDescriptor,
)
from connector.runtime_protocol.host import RuntimeHostClient
from connector.runtimes.cli_headless.runtime import HeadlessCliRuntime, HeadlessCliSpec

CONFIG_SCHEMA_REVISION = 2

_CAPABILITIES: dict[str, bool] = {
    "modelCatalog": True,
    "permissionCatalog": False,
    "sessionDiscovery": False,
    "sessionSnapshot": True,
    "sessionState": True,
    "sessionNotices": False,
    "createAndStartSession": True,
    "startTurn": True,
    "steerTurn": False,
    "interruptTurn": True,
    "commands": False,
    "interactions": False,
    "attachments": False,
    "ipc": False,
}


class HeadlessCliProvider(RuntimeProvider):
    """Provider for one headless CLI kernel described by a HeadlessCliSpec."""

    def __init__(self, spec: HeadlessCliSpec) -> None:
        self._spec = spec

    @property
    def runtime_type(self) -> str:
        return self._spec.key

    @property
    def display_name(self) -> str:
        return self._spec.display_name

    @property
    def description(self) -> str | None:
        return self._spec.description

    @property
    def instance_policy(self) -> str:
        return "single"

    @property
    def max_instances(self) -> int | None:
        return 1

    async def discover(self) -> RuntimeTypeDescriptor:
        available = self._spec.available()
        return RuntimeTypeDescriptor(
            runtime_type=self._spec.key,
            display_name=self._spec.display_name,
            description=self._spec.description,
            available=available,
            recommended=False,
            recommendation_rank=5,
            capabilities=dict(_CAPABILITIES),
            reason=None if available else f"{self._spec.display_name} CLI not found",
            config_schema=await self.get_config_schema(),
            instance_policy="single",
            max_instances=1,
            metadata={"platform": sys.platform, "kind": "headless-cli"},
        )

    async def get_config_schema(self) -> RuntimeConfigSchema:
        properties: dict[str, Any] = {
            "workspaceDir": {
                "type": "string",
                "description": "Default working directory for new sessions",
            },
        }
        ui_schema: dict[str, Any] = {
            "order": ["defaultModel", "workspaceDir"],
            "workspaceDir": {"component": "path"},
        }
        defaults: dict[str, Any] = {}
        if self._spec.models:
            properties["defaultModel"] = {
                "type": "string",
                "enum": [model_id for model_id, _title in self._spec.models],
                "description": "新会话默认使用的模型",
            }
            ui_schema["defaultModel"] = {
                "component": "select",
                "options": [
                    {"value": model_id, "label": title}
                    for model_id, title in self._spec.models
                ],
            }
            defaults["defaultModel"] = self._spec.models[0][0]
        return RuntimeConfigSchema(
            runtime=self._spec.key,
            revision=CONFIG_SCHEMA_REVISION,
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
            },
            ui_schema=ui_schema,
            defaults=defaults,
        )

    async def validate_config(
        self,
        values: dict[str, Any],
    ) -> RuntimeConfig:
        raw = dict(values or {})
        workspace = raw.get("workspaceDir")
        if isinstance(workspace, str) and workspace.strip():
            raw["workspaceDir"] = os.path.abspath(os.path.expanduser(workspace.strip()))
        else:
            raw.pop("workspaceDir", None)
        model_ids = {model_id for model_id, _title in self._spec.models}
        default_model = raw.get("defaultModel")
        if isinstance(default_model, str) and default_model in model_ids:
            raw["defaultModel"] = default_model
        else:
            raw.pop("defaultModel", None)
        schema_bundle = await self.get_config_schema()
        return RuntimeConfig(
            runtime=self._spec.key,
            revision=CONFIG_SCHEMA_REVISION,
            values=raw,
            schema=schema_bundle.schema,
            ui_schema=schema_bundle.ui_schema,
            metadata={"kind": "headless-cli"},
        )

    async def create_runtime(
        self,
        config: RuntimeConfig,
        host: RuntimeHostClient,
    ) -> HeadlessCliRuntime:
        return HeadlessCliRuntime(config=config, host=host, spec=self._spec)


# ---------------------------------------------------------------------- MiniMax


def _minimax_cli() -> str | None:
    """Locate the MiniMax Code CLI (`mcode`), npm-global install or PATH.

    Set MINIMAX_CLI_JS to point at a specific cli.js entrypoint.
    """
    override = os.environ.get("MINIMAX_CLI_JS")
    if override and os.path.isfile(override):
        return override
    return shutil.which("mcode")


def _minimax_available() -> bool:
    cli = _minimax_cli()
    if cli is None:
        return False
    if cli.lower().endswith(".js"):
        return shutil.which("node") is not None
    return True


def _minimax_argv(
    prompt: str, workspace: str | None, model: str | None, cli_session: str | None
) -> list[str] | None:
    cli = _minimax_cli()
    if cli is None:
        return None
    head: list[str]
    if cli.lower().endswith(".js"):
        node = shutil.which("node")
        if node is None:
            return None
        head = [node, cli]
    elif cli.lower().endswith((".cmd", ".bat")):
        head = ["cmd", "/d", "/c", cli]
    else:
        head = [cli]
    argv = head + [
        "exec",
        "--prompt-mode",
        "work",
        "--output-format",
        "stream-json",
    ]
    if cli_session:
        argv += ["--session", cli_session]
    argv.append(prompt)
    return argv


class MiniMaxProvider(HeadlessCliProvider):
    def __init__(self) -> None:
        super().__init__(
            HeadlessCliSpec(
                key="minimax",
                display_name="MiniMax",
                description="MiniMax Code (mcode exec --prompt-mode work)",
                available=_minimax_available,
                build_argv=_minimax_argv,
            )
        )


# -------------------------------------------------------------------- CodeBuddy


def _codebuddy_cli() -> str | None:
    return shutil.which("codebuddy")


def _codebuddy_available() -> bool:
    return _codebuddy_cli() is not None


def _codebuddy_argv(
    prompt: str, workspace: str | None, model: str | None, cli_session: str | None
) -> list[str] | None:
    cli = _codebuddy_cli()
    if cli is None:
        return None
    argv = [
        cli,
        "-p",
        "-y",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
    ]
    if model:
        argv += ["--model", model]
    if cli_session:
        # First turn: session does not exist yet -> created with the same id
        # thanks to --resume-create-missing. Later turns resume it.
        argv += ["--resume", cli_session, "--resume-create-missing"]
    argv.append(prompt)
    if cli.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/d", "/c"] + argv
    return argv


_CODEBUDDY_MODELS: tuple[tuple[str, str], ...] = (
    ("hy4-preview", "Hy4 Preview（默认·推理档·最强但慢）"),
    ("glm-5.3-flashx", "GLM-5.3 FlashX（最快）"),
    ("glm-5.3-flash", "GLM-5.3 Flash（快）"),
    ("deepseek-v4.1-flash", "DeepSeek V4.1 Flash（快）"),
    ("glm-5.2", "GLM-5.2（均衡）"),
    ("kimi-k2.8-preview", "Kimi K2.8 Preview"),
)


class CodeBuddyProvider(HeadlessCliProvider):
    def __init__(self) -> None:
        super().__init__(
            HeadlessCliSpec(
                key="codebuddy",
                display_name="CodeBuddy",
                description="CodeBuddy CLI (codebuddy -p -y; run `codebuddy /login` once first)",
                available=_codebuddy_available,
                build_argv=_codebuddy_argv,
                models=_CODEBUDDY_MODELS,
            )
        )
