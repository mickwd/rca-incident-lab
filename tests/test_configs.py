"""Consistencia de la configuración. Docker no corre en todos lados, así que
se valida estáticamente que nombres de servicios, puertos, rutas y métricas
coincidan entre compose, Prometheus, Grafana, nginx, labctl y el código."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_yaml(rel: str):
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compose() -> dict:
    return load_yaml("docker-compose.yml")


@pytest.fixture(scope="module")
def prometheus() -> dict:
    return load_yaml("prometheus/prometheus.yml")


@pytest.fixture(scope="module")
def dashboard() -> dict:
    return json.loads((ROOT / "grafana/dashboards/rca-overview.json").read_text(encoding="utf-8"))


def test_compose_has_expected_services(compose):
    expected = {
        "certgen",
        "nginx",
        "api",
        "worker",
        "postgres",
        "redis",
        "loadgen",
        "prometheus",
        "grafana",
        "postgres-exporter",
        "redis-exporter",
        "node-exporter",
        "cadvisor",
        "blackbox-exporter",
    }
    assert expected <= set(compose["services"])
    assert compose["name"] == "rca-lab"


def test_compose_depends_on_existing_services(compose):
    services = compose["services"]
    for name, svc in services.items():
        deps = svc.get("depends_on", [])
        for dep in deps if isinstance(deps, list) else deps.keys():
            assert dep in services, f"{name} depende de {dep} que no existe"


def test_compose_build_contexts_exist(compose):
    for name, svc in compose["services"].items():
        if "build" in svc:
            ctx = ROOT / svc["build"]
            assert (ctx / "Dockerfile").exists(), f"{name}: falta Dockerfile en {ctx}"


def test_compose_bind_mounts_exist(compose):
    for name, svc in compose["services"].items():
        for vol in svc.get("volumes", []):
            if isinstance(vol, str) and vol.startswith("./"):
                src = vol.split(":")[0]
                if src == "./certs":
                    continue  # lo genera certgen
                assert (ROOT / src).exists(), f"{name}: no existe {src}"


def test_api_fault_settings(compose):
    api = compose["services"]["api"]
    assert api["mem_limit"] == "256m", "memory-leak depende del límite de memoria"
    assert "SYS_PTRACE" in api["cap_add"], "py-spy necesita ptrace"
    tmpfs = [v for v in api["volumes"] if isinstance(v, dict) and v["type"] == "tmpfs"]
    assert tmpfs and tmpfs[0]["target"] == "/data/uploads"
    assert api["environment"]["UPLOADS_DIR"] == "/data/uploads"
    assert api["environment"]["APP_NAME"] == "orders-api"
    assert compose["services"]["worker"]["environment"]["APP_NAME"] == "orders-worker"


def test_disk_full_filler_lives_in_tmpfs():
    from labctl.scenarios import FILLER_PATH

    assert FILLER_PATH.startswith("/data/uploads/")


def test_postgres_preloads_pg_stat_statements(compose):
    cmd = " ".join(compose["services"]["postgres"]["command"])
    assert "shared_preload_libraries=pg_stat_statements" in cmd
    assert "log_lock_waits=on" in cmd
    schema = (ROOT / "db/init/01-schema.sql").read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS pg_stat_statements" in schema


def test_seed_creates_index_used_by_slow_query():
    seed = (ROOT / "db/init/02-seed.sql").read_text(encoding="utf-8")
    assert "CREATE INDEX ix_orders_customer_id ON orders (customer_id)" in seed


def test_dns_override_only_touches_api(compose):
    override = load_yaml("scenarios/dns-broken/compose.fault.yml")
    assert set(override["services"]) == {"api"}
    bad_host = override["services"]["api"]["environment"]["DB_HOST"]
    assert bad_host not in compose["services"]
    assert compose["services"]["api"]["environment"]["DB_HOST"] == "postgres"


def _scrape_targets(prometheus) -> dict[str, list[str]]:
    return {
        job["job_name"]: [t for sc in job.get("static_configs", []) for t in sc["targets"]]
        for job in prometheus["scrape_configs"]
    }


def test_prometheus_targets_are_compose_services(prometheus, compose):
    services = set(compose["services"])
    for job, targets in _scrape_targets(prometheus).items():
        for target in targets:
            is_url = target.startswith("https://")
            host = target.split("/")[2] if is_url else target.split(":")[0]
            if host == "localhost":
                continue
            assert host in services, f"job {job}: {host} no es un servicio de compose"


def test_prometheus_ports_match_code(prometheus):
    targets = _scrape_targets(prometheus)
    assert targets["orders-api"] == ["api:8000"]
    assert targets["orders-worker"] == ["worker:9101"]
    assert targets["loadgen"] == ["loadgen:9102"]
    worker = (ROOT / "services/orders/orders/worker.py").read_text(encoding="utf-8")
    assert "start_http_server(9101)" in worker
    dockerfile = (ROOT / "services/orders/Dockerfile").read_text(encoding="utf-8")
    assert '"--port", "8000"' in dockerfile


def test_blackbox_modules_exist(prometheus):
    modules = set(load_yaml("blackbox/blackbox.yml")["modules"])
    for job in prometheus["scrape_configs"]:
        for module in job.get("params", {}).get("module", []):
            assert module in modules


def test_rule_files_globs_match_files(prometheus):
    globs = prometheus["rule_files"]
    # cada escenario se lista a mano porque Prometheus no acepta comodines en carpetas
    for alerts in (ROOT / "scenarios").glob("*/alerts.yml"):
        assert alerts.relative_to(ROOT).as_posix() in globs
    for pattern in globs:
        assert "*" not in pattern.rsplit("/", 1)[0], f"{pattern} tiene comodin en la carpeta"
    for pattern in globs:
        base = ROOT / "prometheus" if pattern.startswith("rules/") else ROOT
        assert list(base.glob(pattern)), f"rule_files {pattern} no matchea nada"


def test_prometheus_mounts_rules_where_config_expects(compose):
    vols = compose["services"]["prometheus"]["volumes"]
    assert "./prometheus/rules:/etc/prometheus/rules:ro" in vols
    assert "./scenarios:/etc/prometheus/scenarios:ro" in vols


def _all_rules():
    paths = sorted((ROOT / "scenarios").glob("*/alerts.yml")) + sorted(
        (ROOT / "prometheus/rules").glob("*.yml")
    )
    for path in paths:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for group in data["groups"]:
            yield from group["rules"]


def test_alert_jobs_exist(prometheus):
    jobs = set(_scrape_targets(prometheus))
    for rule in _all_rules():
        for job in re.findall(r'job="([^"]+)"', rule["expr"]):
            assert job in jobs, f"{rule['alert']} usa job={job} que no existe"


def test_alert_exprs_are_balanced():
    for rule in _all_rules():
        expr = rule["expr"]
        for o, c in ("()", "[]", "{}"):
            assert expr.count(o) == expr.count(c), f"{rule['alert']}: {o}{c} desbalanceados"


APP_METRICS = {
    "http_requests_total",
    "http_request_duration_seconds_bucket",
    "http_requests_in_progress",
    "app_exceptions_total",
    "db_pool_checked_out",
    "db_pool_capacity",
    "app_dependency_up",
    "uploads_disk_free_bytes",
    "uploads_disk_total_bytes",
    "orders_created_total",
    "cache_requests_total",
}


def test_custom_metrics_used_in_rules_and_dashboard_exist(dashboard):
    source = (ROOT / "services/orders/orders/metrics.py").read_text(encoding="utf-8")
    for metric in APP_METRICS:
        base = metric.removesuffix("_bucket").removesuffix("_total")
        assert f'"{base}' in source, f"{metric} no está definida en metrics.py"
    exprs = [r["expr"] for r in _all_rules()] + [
        t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])
    ]
    used = {m for e in exprs for m in re.findall(r"[a-z_]+", e)}
    unknown_app = {
        m for m in used if m.startswith(("http_", "app_", "db_pool", "uploads_", "orders_"))
    } - APP_METRICS
    assert not unknown_app, f"métricas inexistentes: {unknown_app}"


def test_grafana_dashboard(dashboard):
    ds = load_yaml("grafana/provisioning/datasources/prometheus.yml")["datasources"][0]
    assert dashboard["uid"] == "rca-overview"
    panels = [p for p in dashboard["panels"] if p["type"] != "row"]
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids))
    titles = " ".join(p["title"].lower() for p in panels)
    for word in ("p50", "error", "rps", "pool", "memoria", "disco", "cpu"):
        assert word in titles, f"falta panel de {word}"
    for p in panels:
        assert p["datasource"]["uid"] == ds["uid"]
        assert p["targets"], f"panel sin queries: {p['title']}"


def test_grafana_provider_path_matches_mount(compose):
    provider = load_yaml("grafana/provisioning/dashboards/provider.yml")["providers"][0]
    mounts = compose["services"]["grafana"]["volumes"]
    assert f"./grafana/dashboards:{provider['options']['path']}:ro" in mounts


def test_nginx_matches_certgen_and_api():
    conf = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
    assert "http://api:8000" in conf
    assert "resolver 127.0.0.11" in conf
    script = (ROOT / "services/certgen/gen-certs.sh").read_text(encoding="utf-8")
    for name in ("server.crt", "server.key"):
        assert f"/etc/nginx/certs/{name}" in conf
        assert name in script
    assert "-startdate" in script and "-enddate" in script


def test_env_example_covers_compose_variables(compose):
    raw = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    used = set(re.findall(r"\$\{([A-Z_]+)(?::-[^}]*)?\}", raw))
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    defined = set(re.findall(r"^([A-Z_]+)=", example, flags=re.MULTILINE))
    assert used <= defined, f"faltan en .env.example: {used - defined}"


def test_ci_workflow():
    ci = load_yaml(".github/workflows/ci.yml")
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "actions/checkout@v5" in text
    assert "actions/setup-python@v6" in text
    jobs = ci["jobs"]
    assert {"lint", "test", "compose", "promtool", "smoke"} <= set(jobs)
    matrix = jobs["test"]["strategy"]["matrix"]["python-version"]
    assert matrix == ["3.11", "3.12", "3.13"]
    assert "down -v" in text


def test_gitattributes_and_gitignore():
    assert "* text=auto eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in (".venv", "__pycache__", ".env", ".coverage", ".pytest_cache"):
        assert entry in ignore
