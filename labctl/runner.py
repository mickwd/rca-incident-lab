"""Ejecución de comandos externos (docker compose)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandError(RuntimeError):
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        detail = (result.stderr or result.stdout).strip()
        super().__init__(f"falló `{' '.join(result.args)}` (rc={result.returncode}): {detail}")


class Runner(Protocol):
    def run(
        self, args: list[str], input: str | None = None, timeout: float = 120
    ) -> CommandResult: ...


class SubprocessRunner:
    def run(self, args: list[str], input: str | None = None, timeout: float = 120) -> CommandResult:
        try:
            proc = subprocess.run(  # noqa: S603 - argumentos armados por labctl, sin shell
                args,
                input=input,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            return CommandResult(tuple(args), 127, "", f"no se encontró el ejecutable {args[0]}")
        except subprocess.TimeoutExpired:
            return CommandResult(tuple(args), 124, "", f"timeout de {timeout}s")
        return CommandResult(tuple(args), proc.returncode, proc.stdout, proc.stderr)
