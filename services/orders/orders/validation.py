"""Validación del campo ``reference`` de las órdenes.

Hay dos versiones. ``validate_reference_v1`` es la correcta. La v2 simula un
cambio desplegado que "simplificó" la regex y quedó vulnerable a backtracking
catastrófico (ReDoS): el grupo ``([A-Za-z0-9]+[-_ ]?)*`` admite exponenciales
formas de partir una cadena alfanumérica larga, y cuando al final hay un
caracter inválido el motor prueba todas antes de fallar. El módulo ``re`` de
Python no suelta el GIL mientras matchea, así que un solo request congela
todo el proceso.
"""

from __future__ import annotations

import re

MAX_REFERENCE_LENGTH = 64

# Segura: los separadores son obligatorios entre bloques, no hay ambigüedad
SAFE_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:[-_ ][A-Za-z0-9]+)*$")

# Vulnerable: separador opcional dentro de un grupo repetido
VULNERABLE_PATTERN = re.compile(r"^([A-Za-z0-9]+[-_ ]?)*$")


def validate_reference_v1(value: str) -> bool:
    if not value or len(value) > MAX_REFERENCE_LENGTH:
        return False
    return SAFE_PATTERN.match(value) is not None


def validate_reference_v2(value: str) -> bool:
    # Sin límite de largo y con la regex vulnerable
    if not value:
        return False
    return VULNERABLE_PATTERN.match(value) is not None


def validate_reference(value: str, use_v2: bool) -> bool:
    if use_v2:
        return validate_reference_v2(value)
    return validate_reference_v1(value)
