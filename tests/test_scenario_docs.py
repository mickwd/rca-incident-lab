"""Cada escenario tiene README (runbook), postmortem y alertas con la estructura acordada."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from labctl.scenarios import SCENARIOS

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = ROOT / "scenarios"

README_SECTIONS = [
    "Síntomas",
    "Runbook de diagnóstico",
    "Consultas PromQL",
    "Confirmar la causa raíz",
    "Mitigación inmediata",
    "Solución definitiva",
    "Alertas",
    "Cómo reproducir",
]
POSTMORTEM_SECTIONS = [
    "Resumen",
    "Impacto",
    "Línea de tiempo",
    "Causa raíz",
    "5 porqués",
    "Qué salió bien",
    "Qué salió mal",
    "Action items",
]


def h2_sections(markdown: str) -> dict[str, str]:
    """Devuelve {título H2: contenido}, ignorando bloques de código."""
    sections: dict[str, str] = {}
    current = None
    in_code = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        if not in_code and line.startswith("## "):
            current = line[3:].strip()
            sections[current] = ""
        elif current is not None:
            sections[current] += line + "\n"
    return sections


def h1_title(markdown: str) -> str | None:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def in_order(found: list[str], required: list[str]) -> bool:
    positions = [found.index(r) for r in required]
    return positions == sorted(positions)


def test_scenario_folders_match_labctl_registry():
    folders = {p.name for p in SCENARIOS_DIR.iterdir() if p.is_dir()}
    assert folders == set(SCENARIOS)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_readme_has_required_sections(name):
    text = (SCENARIOS_DIR / name / "README.md").read_text(encoding="utf-8")
    assert h1_title(text)
    sections = h2_sections(text)
    missing = [s for s in README_SECTIONS if s not in sections]
    assert not missing, f"faltan secciones en {name}/README.md: {missing}"
    assert in_order(list(sections), README_SECTIONS)
    for s in README_SECTIONS:
        assert sections[s].strip(), f"sección vacía: {s}"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_runbook_has_commands(name):
    text = (SCENARIOS_DIR / name / "README.md").read_text(encoding="utf-8")
    runbook = h2_sections(text)["Runbook de diagnóstico"]
    assert runbook.count("```") >= 4, "el runbook necesita comandos concretos"
    assert f"labctl break {name}" in text
    assert f"labctl fix {name}" in text


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_postmortem_has_required_sections(name):
    text = (SCENARIOS_DIR / name / "postmortem.md").read_text(encoding="utf-8")
    title = h1_title(text)
    assert title and title.startswith("Postmortem")
    sections = h2_sections(text)
    missing = [s for s in POSTMORTEM_SECTIONS if s not in sections]
    assert not missing, f"faltan secciones en {name}/postmortem.md: {missing}"
    assert in_order(list(sections), POSTMORTEM_SECTIONS)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_postmortem_five_whys(name):
    text = (SCENARIOS_DIR / name / "postmortem.md").read_text(encoding="utf-8")
    whys = h2_sections(text)["5 porqués"]
    numbered = re.findall(r"^\s*\d+\.\s+\S", whys, flags=re.MULTILINE)
    assert len(numbered) >= 5


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_postmortem_action_items_have_owner_and_priority(name):
    text = (SCENARIOS_DIR / name / "postmortem.md").read_text(encoding="utf-8")
    block = h2_sections(text)["Action items"]
    rows = [r for r in block.splitlines() if r.strip().startswith("|")]
    header = [c.strip() for c in rows[0].strip("| ").split("|")]
    assert "Dueño" in header and "Prioridad" in header
    owner_i, prio_i = header.index("Dueño"), header.index("Prioridad")
    data = rows[2:]
    assert len(data) >= 3
    for row in data:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        assert cells[owner_i], f"action item sin dueño: {row}"
        assert re.fullmatch(r"P[0-3]", cells[prio_i]), f"prioridad inválida: {row}"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_alerts_file(name):
    data = yaml.safe_load((SCENARIOS_DIR / name / "alerts.yml").read_text(encoding="utf-8"))
    rules = [r for g in data["groups"] for r in g["rules"]]
    assert rules
    for rule in rules:
        assert re.fullmatch(r"[A-Z][A-Za-z0-9]+", rule["alert"])
        assert rule["expr"].strip()
        assert rule["labels"]["severity"] in {"warning", "critical"}
        assert rule["labels"]["scenario"] == name
        for key in ("summary", "description", "runbook_url"):
            assert rule["annotations"].get(key), f"{rule['alert']} sin {key}"
        assert rule["annotations"]["runbook_url"] == f"scenarios/{name}/README.md"


def test_alert_names_are_unique():
    names: list[str] = []
    for path in sorted(SCENARIOS_DIR.glob("*/alerts.yml")) + sorted(
        (ROOT / "prometheus" / "rules").glob("*.yml")
    ):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        names += [r["alert"] for g in data["groups"] for r in g["rules"]]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("doc", ["metodologia.md", "plantilla-postmortem.md", "glosario.md"])
def test_docs_exist(doc):
    text = (ROOT / "docs" / doc).read_text(encoding="utf-8")
    assert h1_title(text)
    assert len(text) > 1500


def test_postmortem_template_has_required_sections():
    text = (ROOT / "docs" / "plantilla-postmortem.md").read_text(encoding="utf-8")
    sections = h2_sections(text)
    assert all(s in sections for s in POSTMORTEM_SECTIONS)


def test_readme_links_every_scenario():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in SCENARIOS:
        assert f"scenarios/{name}" in text
    assert "```mermaid" in text
    assert "labctl break random" in text
    assert "labctl status --reveal" in text
