"""Operaciones sobre el stack: compose, flags en Redis, SQL y probes HTTP."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .runner import CommandError, CommandResult, Runner

_PACKAGE_PARENT = Path(__file__).resolve().parent.parent
# Instalado en modo editable la raíz es el repo. Si no, se usa el directorio actual
ROOT = _PACKAGE_PARENT if (_PACKAGE_PARENT / "docker-compose.yml").exists() else Path.cwd()
FLAG_PREFIX = "fault:"


@dataclass(frozen=True)
class ProbeResult:
    path: str
    status: int | None
    elapsed_ms: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200


class HttpClient(Protocol):
    def request(
        self, method: str, url: str, json: dict | None = None, timeout: float = 5.0
    ) -> int: ...


class HttpxClient:
    def __init__(self, cacert: Path | None) -> None:
        self._verify: str | bool = str(cacert) if cacert and cacert.exists() else True

    def request(self, method: str, url: str, json: dict | None = None, timeout: float = 5.0) -> int:
        import httpx

        resp = httpx.request(method, url, json=json, timeout=timeout, verify=self._verify)
        return resp.status_code


@dataclass
class LabConfig:
    root: Path = ROOT
    base_url: str = "https://localhost:8443"
    compose_file: Path | None = None
    cacert: Path | None = None
    state_file: Path | None = None

    def __post_init__(self) -> None:
        self.compose_file = self.compose_file or self.root / "docker-compose.yml"
        self.cacert = self.cacert or self.root / "certs" / "ca.crt"
        self.state_file = self.state_file or self.root / ".labctl" / "state.json"


class Lab:
    def __init__(self, config: LabConfig, runner: Runner, http: HttpClient) -> None:
        self.config = config
        self.runner = runner
        self.http = http

    # --- docker compose -------------------------------------------------
    def compose_args(self, *args: str, files: tuple[Path, ...] = ()) -> list[str]:
        cmd = [
            "docker",
            "compose",
            "--project-directory",
            str(self.config.root),
            "-f",
            str(self.config.compose_file),
        ]
        for extra in files:
            cmd += ["-f", str(extra)]
        return cmd + list(args)

    def run(
        self, args: list[str], input: str | None = None, check: bool = True, timeout: float = 180
    ) -> CommandResult:
        result = self.runner.run(args, input=input, timeout=timeout)
        if check and not result.ok:
            raise CommandError(result)
        return result

    def exec(
        self, service: str, command: list[str], input: str | None = None, check: bool = True
    ) -> CommandResult:
        return self.run(
            self.compose_args("exec", "-T", service, *command), input=input, check=check
        )

    def restart(self, service: str) -> CommandResult:
        return self.run(self.compose_args("restart", service))

    def recreate(self, service: str, files: tuple[Path, ...] = ()) -> CommandResult:
        return self.run(
            self.compose_args("up", "-d", "--no-deps", "--force-recreate", service, files=files)
        )

    def certgen(self, mode: str) -> CommandResult:
        return self.run(self.compose_args("run", "--rm", "certgen", mode))

    def reload_nginx(self) -> CommandResult:
        return self.exec("nginx", ["nginx", "-s", "reload"])

    # --- flags en Redis -------------------------------------------------
    def set_flag(self, name: str, value: str = "1") -> None:
        self.exec("redis", ["redis-cli", "SET", FLAG_PREFIX + name, value])

    def clear_flag(self, name: str) -> None:
        self.exec("redis", ["redis-cli", "DEL", FLAG_PREFIX + name])

    def list_flags(self) -> list[str]:
        result = self.exec(
            "redis", ["redis-cli", "--scan", "--pattern", FLAG_PREFIX + "*"], check=False
        )
        if not result.ok:
            return []
        return sorted(
            line.strip().removeprefix(FLAG_PREFIX)
            for line in result.stdout.splitlines()
            if line.strip()
        )

    # --- SQL --------------------------------------------------------------
    def psql(self, sql: str) -> str:
        # El SQL va por stdin para no pelear con el quoting de cada shell
        result = self.exec(
            "postgres",
            ["sh", "-c", 'psql -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d "$POSTGRES_DB"'],
            input=sql,
        )
        return result.stdout

    # --- HTTP -------------------------------------------------------------
    def probe(self, path: str, timeout: float = 5.0) -> ProbeResult:
        start = time.perf_counter()
        try:
            status = self.http.request("GET", self.config.base_url + path, timeout=timeout)
            return ProbeResult(path, status, (time.perf_counter() - start) * 1000)
        except Exception as exc:  # noqa: BLE001 - se informa el error tal cual
            return ProbeResult(path, None, (time.perf_counter() - start) * 1000, _short(exc))

    def post_quiet(self, path: str, payload: dict, timeout: float = 2.0) -> int | None:
        try:
            return self.http.request("POST", self.config.base_url + path, payload, timeout)
        except Exception:  # noqa: BLE001 - el timeout es el resultado esperado
            return None


def _short(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    if "CERTIFICATE_VERIFY_FAILED" in text:
        reason = text.split("CERTIFICATE_VERIFY_FAILED", 1)[1].strip(" ]:()")
        return f"TLS: CERTIFICATE_VERIFY_FAILED {reason}".strip()
    return f"{type(exc).__name__}: {text}"[:200]
