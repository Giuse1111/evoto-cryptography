"""
Verificatore indipendente da riga di comando.

Uso:
    uv run python -m verifica registro.json

Legge il registro pubblico JSON ed esegue i controlli V1-V8 senza
importare la libreria evoto. Il codice di uscita vale 0 se l'elezione
è verificata, 1 se almeno un controllo fallisce, 2 se il file non si
può leggere.
"""

import argparse
from pathlib import Path
import sys
import time

from verifica.verifica import verify_public_registry


CHECK_DESCRIPTIONS = {
    "V1": "Parametri del gruppo e chiave pubblica",
    "V2": "Prove di Schnorr dei garanti, chiave congiunta, Q e Q_bar",
    "V3": "Prove R1-R5 di ogni scheda",
    "V4": "Catena dei codici, schede sprecate ricifrate, nessuna copia",
    "V5": "Totali cifrati ricalcolati dalle sole schede depositate",
    "V6": "Prove Chaum-Pedersen delle share di decifratura e quorum",
    "V7": "Totali in chiaro compatibili con i totali cifrati: B / M = g^t",
    "V8": "Scrutinio rifatto da zero: soglie, premio, seggi ed eletti",
}


def main(argv: list[str] | None = None) -> int:
    """
    Verifica il registro indicato e stampa l'esito di ogni controllo.
    """

    parser = argparse.ArgumentParser(
        prog="python -m verifica",
        description="Verificatore indipendente del registro pubblico di eVoto.",
    )

    parser.add_argument(
        "registro",
        help="file JSON del registro pubblico",
    )

    arguments = parser.parse_args(argv)

    try:
        serialized = Path(arguments.registro).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        print(
            f"Impossibile leggere {arguments.registro}: {error}",
            file=sys.stderr,
        )
        return 2

    start = time.perf_counter()
    verdict = verify_public_registry(serialized)
    elapsed = time.perf_counter() - start

    print(f"\nRegistro: {arguments.registro}\n")

    for name, description in CHECK_DESCRIPTIONS.items():
        outcome = "OK  " if verdict[name] else "FAIL"
        print(f"  {outcome}  {name}  {description}")

    print()

    if verdict["overall"]:
        print("ELEZIONE VERIFICATA")
    else:
        print("ATTENZIONE: VERIFICA FALLITA")

    print(f"Tempo: {elapsed:.1f} s\n")

    return 0 if verdict["overall"] else 1


if __name__ == "__main__":
    sys.exit(main())
