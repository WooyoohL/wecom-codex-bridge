import os
import re
import time
import urllib.request

from wecom_bridge.config import Config
from wecom_bridge.models import SlashCommandInfo


class CodexSlashCommandProvider:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._cache: list[SlashCommandInfo] | None = None
        self._cache_at = 0.0
        self._source = ""

    @property
    def source(self) -> str:
        return self._source

    def list_commands(self) -> list[SlashCommandInfo]:
        now = time.time()
        if (
            self._cache is not None
            and now - self._cache_at < self.config.codex_slash_commands_cache_seconds
        ):
            return self._cache

        text = self._fetch_slash_command_source()
        commands = self._parse_rust_slash_commands(text)
        if not commands:
            raise RuntimeError("could not parse Codex slash_command.rs")

        self._cache = commands
        self._cache_at = now
        return commands

    def _fetch_slash_command_source(self) -> str:
        url = self.config.codex_slash_commands_url
        request = urllib.request.Request(url, headers={"User-Agent": "wecom-codex-bridge"})
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
        self._source = url
        return body

    def _parse_rust_slash_commands(self, text: str) -> list[SlashCommandInfo]:
        enum_match = re.search(
            r"pub\s+enum\s+SlashCommand\s*\{(?P<body>.*?)\}\s*impl\s+SlashCommand",
            text,
            flags=re.DOTALL,
        )
        if not enum_match:
            return []

        descriptions = self._parse_descriptions(text)
        enum_body = re.sub(r"//.*", "", enum_match.group("body"))
        commands: list[SlashCommandInfo] = []

        for raw_item in enum_body.split(","):
            item = raw_item.strip()
            if not item:
                continue

            attrs = re.findall(r"#\[strum\((.*?)\)\]", item)
            without_attrs = re.sub(r"#\[.*?\]", "", item).strip()
            variant_match = re.search(r"\b([A-Z][A-Za-z0-9_]*)\b", without_attrs)
            if not variant_match:
                continue

            variant = variant_match.group(1)
            command = self._command_from_attrs_or_variant(attrs, variant)
            description = descriptions.get(variant, "")

            if self._hide_command(variant, command, description):
                continue

            commands.append(SlashCommandInfo(command=command, description=description))

        return commands

    def _parse_descriptions(self, text: str) -> dict[str, str]:
        match_body = re.search(
            r"pub\s+fn\s+description\(self\).*?match\s+self\s*\{(?P<body>.*?)\}\s*\}\s*///\s*Command string",
            text,
            flags=re.DOTALL,
        )
        if not match_body:
            return {}

        descriptions: dict[str, str] = {}
        pattern = re.compile(
            r"(?P<arms>SlashCommand::[A-Za-z0-9_]+(?:\s*\|\s*SlashCommand::[A-Za-z0-9_]+)*)"
            r"\s*=>\s*(?:\{\s*)?\"(?P<description>[^\"]*)\"",
            flags=re.DOTALL,
        )
        for match in pattern.finditer(match_body.group("body")):
            variants = re.findall(r"SlashCommand::([A-Za-z0-9_]+)", match.group("arms"))
            for variant in variants:
                descriptions[variant] = match.group("description")
        return descriptions

    def _command_from_attrs_or_variant(self, attrs: list[str], variant: str) -> str:
        for attr in attrs:
            match = re.search(r'to_string\s*=\s*"([^"]+)"', attr)
            if match:
                return match.group(1)
        for attr in attrs:
            match = re.search(r'serialize\s*=\s*"([^"]+)"', attr)
            if match:
                return match.group(1)
        return camel_to_kebab(variant)

    def _hide_command(self, variant: str, command: str, description: str) -> bool:
        if variant in {"MemoryDrop", "MemoryUpdate", "Rollout", "TestApproval"}:
            return True
        if command == "sandbox-add-read-dir" and os.name != "nt":
            return True
        return description == "DO NOT USE"


def camel_to_kebab(value: str) -> str:
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+|[0-9]+", value)
    return "-".join(word.lower() for word in words)
