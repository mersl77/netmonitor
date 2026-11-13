"""QR kód jako matice modulů (True = tmavý). Vykreslení řeší GUI."""
from __future__ import annotations


def matrix(text: str) -> list[list[bool]]:
    import segno
    code = segno.make(text, error="m", micro=False)
    return [[bool(cell) for cell in row] for row in code.matrix]
