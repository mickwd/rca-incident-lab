"""labctl con clientes falsos de Docker y HTTP: se valida qué comandos ejecuta."""

from __future__ import annotations

from pathlib import Path

import pytest

from labctl.cli import main
from labctl.lab import Lab, LabConfig
from labctl.runner import CommandResult
from labctl.scenarios import EVIL_REFERENCE, SCENARIOS
from labctl.state import StateStore

ROOT = Path(__file__).resolve().parent.parent


class FakeRunner:
    def __init__(self, fail_on: str | None = None, stdout: str = "") -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail_on = fail_on
        self.stdout = stdout

    def run(self, args, input=None, timeout=120):
        self.calls.append((list(args), input))
        if self.fail_on and self.fail_on in " ".join(args):
            return CommandResult(tuple(args), 1, "", "boom")
        return CommandResult(tuple(args), 0, self.stdout, "")

    def commands(self) -> list[str]:
        """Comandos sin el prefijo `docker compose --project-directory X -f Y`."""
        out = []
        for args, _ in self.calls:
            rest = args[2:]
            while rest and rest[0] in ("--project-directory", "-f"):
                rest = rest[2:]
            out.append(" ".join(rest))
        return out

    def inputs(self) -> list[str]:
        return [i for _, i in self.calls if i]


class FakeHttp:
    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.requests: list[tuple[str, str, dict | None]] = []

    def request(self, method, url, json=None, timeout=5.0):
        self.requests.append((method, url, json))
        if self.error:
            raise self.error
        return self.status


@pytest.fixture
def env(tmp_path):
    runner = FakeRunner()
    http = FakeHttp()
    config = LabConfig(root=ROOT, state_file=tmp_path / "state.json")
    lab = Lab(config, runner, http)
    store = StateStore(config.state_file)
    lines: list[str] = []

    def run(*argv: str) -> int:
        return main(list(argv), lab=lab, store=store, out=lines.append)

    return runner, http, store, lines, run


def test_registry_has_the_eight_scenarios():
    assert set(SCENARIOS) == {
        "memory-leak",
        "db-lock",
        "connection-pool-exhaustion",
        "disk-full",
        "expired-cert",
        "dns-broken",
        "slow-query",
        "cpu-spike",
    }


def test_list(env):
    *_, lines, run = env
    assert run("list") == 0
    assert len(lines) == 1 + len(SCENARIOS)


def test_compose_prefix_points_to_repo(env):
    runner, *_, run = env
    run("break", "memory-leak")
    args = runner.calls[0][0]
    assert args[:2] == ["docker", "compose"]
    assert args[args.index("-f") + 1] == str(ROOT / "docker-compose.yml")


@pytest.mark.parametrize(
    ("name", "expected_break", "expected_fix"),
    [
        (
            "memory-leak",
            ["exec -T redis redis-cli SET fault:memory-leak 262144"],
            ["exec -T redis redis-cli DEL fault:memory-leak", "restart api"],
        ),
        (
            "db-lock",
            ["exec -T redis redis-cli SET fault:db-lock 1"],
            [
                "exec -T redis redis-cli DEL fault:db-lock",
                'exec -T postgres sh -c psql -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" '
                '-d "$POSTGRES_DB"',
            ],
        ),
        (
            "connection-pool-exhaustion",
            ["exec -T redis redis-cli SET fault:pool-leak 1"],
            ["exec -T redis redis-cli DEL fault:pool-leak", "restart api"],
        ),
        (
            "expired-cert",
            ["run --rm certgen expired", "exec -T nginx nginx -s reload"],
            ["run --rm certgen valid", "exec -T nginx nginx -s reload"],
        ),
        (
            "dns-broken",
            ["up -d --no-deps --force-recreate api"],
            ["up -d --no-deps --force-recreate api"],
        ),
        (
            "cpu-spike",
            ["exec -T redis redis-cli SET fault:cpu-spike 1"],
            ["exec -T redis redis-cli DEL fault:cpu-spike", "restart api"],
        ),
    ],
)
def test_break_and_fix_commands(env, name, expected_break, expected_fix):
    runner, _http, store, _lines, run = env
    assert run("break", name) == 0
    assert runner.commands() == expected_break
    assert [a.name for a in store.active()] == [name]
    runner.calls.clear()
    assert run("fix", name) == 0
    assert runner.commands() == expected_fix
    assert store.active() == []


def test_db_lock_fix_terminates_only_worker_idle_sessions(env):
    runner, *_, run = env
    run("fix", "db-lock")
    sql = runner.inputs()[0]
    assert "pg_terminate_backend" in sql
    assert "application_name = 'orders-worker'" in sql
    assert "idle in transaction" in sql


def test_slow_query_drops_and_recreates_index(env):
    runner, *_, run = env
    run("break", "slow-query")
    assert "DROP INDEX IF EXISTS ix_orders_customer_id" in runner.inputs()[0]
    run("fix", "slow-query")
    fix_sql = runner.inputs()[1]
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_orders_customer_id" in fix_sql
    assert "ANALYZE orders" in fix_sql


def test_dns_broken_uses_override_file(env):
    runner, *_, run = env
    run("break", "dns-broken")
    args = runner.calls[0][0]
    files = [args[i + 1] for i, a in enumerate(args) if a == "-f"]
    override = ROOT / "scenarios" / "dns-broken" / "compose.fault.yml"
    assert files == [str(ROOT / "docker-compose.yml"), str(override)]
    assert override.exists()
    runner.calls.clear()
    run("fix", "dns-broken")
    args = runner.calls[0][0]
    assert [args[i + 1] for i, a in enumerate(args) if a == "-f"] == [
        str(ROOT / "docker-compose.yml")
    ]


def test_disk_full_fill_and_cleanup_same_path(env):
    runner, *_, run = env
    run("break", "disk-full")
    run("fix", "disk-full")
    fill, clean = runner.calls[0][0], runner.calls[1][0]
    path = clean[-1]
    assert path.startswith("/data/uploads/")
    assert path in fill[-1]
    assert "dd if=/dev/zero" in fill[-1]


def test_cpu_spike_sends_evil_payloads(env):
    _runner, http, *_, run = env
    run("break", "cpu-spike")
    posts = [r for r in http.requests if r[0] == "POST"]
    assert len(posts) == 3
    assert all(r[1].endswith("/orders/validate") for r in posts)
    assert posts[0][2] == {"reference": EVIL_REFERENCE}


def test_cpu_spike_tolerates_client_timeouts(tmp_path):
    lab = Lab(
        LabConfig(root=ROOT, state_file=tmp_path / "s.json"),
        FakeRunner(),
        FakeHttp(error=TimeoutError("read timeout")),
    )
    assert main(["break", "cpu-spike"], lab=lab, out=lambda _l: None) == 0


def test_unknown_scenario(env):
    *_, lines, run = env
    assert run("break", "nope") == 2
    assert run("fix", "nope") == 2
    assert "desconocido" in lines[-1]


def test_command_failure_returns_error(tmp_path):
    lines: list[str] = []
    lab = Lab(
        LabConfig(root=ROOT, state_file=tmp_path / "s.json"),
        FakeRunner(fail_on="redis-cli"),
        FakeHttp(),
    )
    assert main(["break", "memory-leak"], lab=lab, out=lines.append) == 1
    assert lines[-1].startswith("ERROR:")
    assert StateStore(tmp_path / "s.json").active() == []


def test_random_is_hidden_until_reveal(env):
    _runner, _http, store, lines, run = env
    assert run("break", "random", "--seed", "3") == 0
    [active] = store.active()
    assert active.hidden
    lines.clear()
    run("status")
    text = "\n".join(lines)
    assert active.name not in text
    assert "???" in text
    lines.clear()
    run("status", "--reveal")
    text = "\n".join(lines)
    assert active.name in text
    assert "Causa raíz" in text


def test_random_is_deterministic_with_seed(tmp_path):
    picks = []
    for i in range(2):
        state = tmp_path / f"s{i}.json"
        lab = Lab(LabConfig(root=ROOT, state_file=state), FakeRunner(), FakeHttp())
        main(["break", "random", "--seed", "42"], lab=lab, out=lambda _l: None)
        picks.append(StateStore(state).active()[0].name)
    assert picks[0] == picks[1]


def test_random_skips_active_scenarios(env):
    _r, _h, store, _lines, run = env
    for _ in range(len(SCENARIOS)):
        assert run("break", "random") == 0
    assert {a.name for a in store.active()} == set(SCENARIOS)
    assert run("break", "random") == 1


def test_fix_all_only_active_unless_forced(env):
    runner, _h, store, lines, run = env
    run("break", "slow-query")
    run("break", "memory-leak")
    runner.calls.clear()
    assert run("fix", "all") == 0
    assert store.active() == []
    assert len([line for line in lines if "revertido" in line]) == 2
    lines.clear()
    assert run("fix", "all", "--force") == 0
    assert len([line for line in lines if "revertido" in line]) == len(SCENARIOS)


def test_status_reports_probe_errors(tmp_path):
    lines: list[str] = []
    err = Exception(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired"
    )
    lab = Lab(
        LabConfig(root=ROOT, state_file=tmp_path / "s.json"), FakeRunner(), FakeHttp(error=err)
    )
    assert main(["status"], lab=lab, out=lines.append) == 0
    assert any("ERROR" in line and "CERTIFICATE_VERIFY_FAILED" in line for line in lines)


def test_reveal_lists_redis_flags(tmp_path):
    lines: list[str] = []
    runner = FakeRunner(stdout="fault:cpu-spike\nfault:db-lock\n")
    lab = Lab(LabConfig(root=ROOT, state_file=tmp_path / "s.json"), runner, FakeHttp())
    main(["status", "--reveal"], lab=lab, out=lines.append)
    assert lines[-1].endswith("cpu-spike, db-lock")
