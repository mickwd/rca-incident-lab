"""labctl: inyección de fallas para el laboratorio de RCA.

Uso:
    labctl list
    labctl break <escenario|random> [--seed N]
    labctl fix <escenario|all> [--force]
    labctl status [--reveal]
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .lab import HttpxClient, Lab, LabConfig
from .runner import CommandError, SubprocessRunner
from .scenarios import SCENARIOS
from .state import StateStore

PROBES = ("/healthz", "/readyz", "/products/1")


def build_lab() -> Lab:
    config = LabConfig(base_url=os.getenv("LAB_BASE_URL", "https://localhost:8443"))
    root = os.getenv("LAB_ROOT")
    if root:
        config = LabConfig(root=Path(root), base_url=config.base_url)
    return Lab(config, SubprocessRunner(), HttpxClient(config.cacert))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="labctl", description="Rompe y arregla el laboratorio de RCA"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="lista los escenarios disponibles")

    p_break = sub.add_parser("break", help="activa un escenario de falla")
    p_break.add_argument("scenario", help="nombre del escenario o 'random' (modo desafío)")
    p_break.add_argument("--seed", type=int, default=None, help="semilla para 'random'")

    p_fix = sub.add_parser("fix", help="revierte un escenario")
    p_fix.add_argument("scenario", help="nombre del escenario o 'all'")
    p_fix.add_argument(
        "--force", action="store_true", help="con 'all' arregla todos aunque no figuren activos"
    )

    p_status = sub.add_parser("status", help="salud del stack y escenarios activos")
    p_status.add_argument("--reveal", action="store_true", help="muestra la causa (modo desafío)")
    return parser


def cmd_list(out: Callable[[str], None]) -> int:
    out(f"{'ESCENARIO':<28} {'CATEGORÍA':<9} SÍNTOMA PRINCIPAL")
    for s in SCENARIOS.values():
        out(f"{s.name:<28} {s.category:<9} {s.title}")
    return 0


def cmd_break(lab: Lab, store: StateStore, name: str, seed: int | None, out) -> int:
    hidden = name == "random"
    if hidden:
        active = {a.name for a in store.active()}
        candidates = sorted(set(SCENARIOS) - active)
        if not candidates:
            out("Todos los escenarios ya están activos")
            return 1
        name = random.Random(seed).choice(candidates)
    scenario = SCENARIOS.get(name)
    if scenario is None:
        out(f"Escenario desconocido: {name}. Usá `labctl list`.")
        return 2
    scenario.break_(lab)
    store.add(scenario.name, hidden=hidden)
    if hidden:
        out("Modo desafío: se activó un escenario aleatorio.")
        out("Seguí docs/metodologia.md: observá, formulá hipótesis, probá y aislá.")
        out("Cuando tengas la causa raíz: labctl status --reveal")
    else:
        out(f"Escenario '{scenario.name}' activado. Runbook: {scenario.folder / 'README.md'}")
    return 0


def cmd_fix(lab: Lab, store: StateStore, name: str, force: bool, out) -> int:
    if name == "all":
        names = list(SCENARIOS) if force else [a.name for a in store.active()]
        if not names:
            out("No hay escenarios activos registrados (usá --force para arreglar todo igual)")
            return 0
    elif name in SCENARIOS:
        names = [name]
    else:
        out(f"Escenario desconocido: {name}. Usá `labctl list`.")
        return 2
    for n in names:
        SCENARIOS[n].fix(lab)
        store.remove(n)
        out(f"Escenario '{n}' revertido")
    return 0


def cmd_status(lab: Lab, store: StateStore, reveal: bool, out) -> int:
    out("Salud vista desde el cliente (a través de nginx):")
    for path in PROBES:
        r = lab.probe(path)
        status = str(r.status) if r.status is not None else "ERROR"
        extra = f"  {r.error}" if r.error else ""
        out(f"  GET {path:<14} -> {status:<5} {r.elapsed_ms:7.0f} ms{extra}")
    active = store.active()
    out(f"Escenarios activos: {len(active)}")
    for a in active:
        if a.hidden and not reveal:
            out("  - ??? (modo desafío, usá --reveal para ver la causa)")
            continue
        s = SCENARIOS[a.name]
        out(f"  - {s.name} ({s.category}) desde {a.since}")
        if reveal:
            out(f"    Causa raíz: {s.root_cause}")
            out(f"    Runbook: {s.folder / 'README.md'}  Postmortem: {s.folder / 'postmortem.md'}")
    if reveal:
        flags = lab.list_flags()
        out(f"Flags fault:* en Redis: {', '.join(flags) if flags else 'ninguno'}")
    return 0


def main(
    argv: Sequence[str] | None = None,
    lab: Lab | None = None,
    store: StateStore | None = None,
    out: Callable[[str], None] = print,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "list":
        return cmd_list(out)
    lab = lab or build_lab()
    store = store or StateStore(lab.config.state_file)
    try:
        if args.command == "break":
            return cmd_break(lab, store, args.scenario, args.seed, out)
        if args.command == "fix":
            return cmd_fix(lab, store, args.scenario, args.force, out)
        return cmd_status(lab, store, args.reveal, out)
    except CommandError as exc:
        out(f"ERROR: {exc}")
        return 1


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
