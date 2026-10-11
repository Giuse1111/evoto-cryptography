"""
Test delle opzioni della demo da riga di comando (demo.py).
"""

import pytest

from demo import present_guardians


def test_default_present_guardians():
    """
    Di default i garanti 1, 3 e 5; se non esistono o non bastano,
    i primi quorum garanti.
    """

    assert present_guardians(None, 5, 3) == (1, 3, 5)
    assert present_guardians(None, 3, 3) == (1, 2, 3)
    assert present_guardians(None, 7, 4) == (1, 2, 3, 4)


def test_explicit_present_guardians():
    """
    L'elenco esplicito viene letto così com'è.
    """

    assert present_guardians("2,4,5", 5, 3) == (2, 4, 5)


@pytest.mark.parametrize(
    "text",
    ["1,3,9", "1,x", "1,1,2", "1,2", "0,1,2"],
)
def test_invalid_present_guardians(text):
    """
    Garanti inesistenti, ripetuti, non numerici o meno del quorum
    vengono rifiutati con un messaggio, non con un errore interno.
    """

    with pytest.raises(ValueError):
        present_guardians(text, 5, 3)
