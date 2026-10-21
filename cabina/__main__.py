"""
Avvia la cabina elettorale dimostrativa nel browser.

Uso:
    uv run python -m cabina
    uv run python -m cabina --gruppo didattico --elettori 100
    uv run python -m cabina --registro registro_demo.json

All'avvio viene preparata la prima simulazione (cerimonia delle chiavi
e voto degli elettori simulati); poi il server resta in ascolto solo sul
computer locale (127.0.0.1). Nel browser si comincia dalla pagina
«Prepara»: lì si conferma questa simulazione oppure se ne prepara
un'altra, senza riavviare il server.
"""

import argparse
import os
from pathlib import Path
import sys
import time

from cabina.app import create_app
from cabina.preparazione import (
    MODES,
    DemoHost,
    SimulationSettings,
)
from evoto.configurazione import load_election_config


DEFAULT_CONFIG = (
    Path(__file__).resolve().parent.parent
    / "config"
    / "elezione_esempio.json"
)

# Gruppo indicato sulla riga di comando -> modalità della pagina Prepara.
GROUP_MODES = {
    "demo": "realistica",
    "didattico": "veloce",
}


def build_parser() -> argparse.ArgumentParser:
    """
    Opzioni della riga di comando.
    """

    parser = argparse.ArgumentParser(
        prog="python -m cabina",
        description="Cabina elettorale e bacheca dimostrative di eVoto.",
    )

    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="file JSON di configurazione dell'elezione",
    )

    parser.add_argument(
        "--gruppo",
        choices=tuple(GROUP_MODES),
        default="demo",
        help="demo (2048 bit, default) oppure didattico (veloce, non sicuro)",
    )

    parser.add_argument(
        "--elettori",
        type=int,
        default=10,
        help=(
            "elettori simulati per circoscrizione (default 10; al massimo "
            f"{MODES['realistica'].max_voters_per_district} con il gruppo "
            f"demo e {MODES['veloce'].max_voters_per_district} con il "
            "gruppo didattico)"
        ),
    )

    parser.add_argument(
        "--garanti",
        type=int,
        default=5,
        help="numero di garanti (default 5)",
    )

    parser.add_argument(
        "--quorum",
        type=int,
        default=3,
        help="garanti necessari per decifrare (default 3)",
    )

    parser.add_argument(
        "--seme",
        type=int,
        default=2026,
        help="seme delle scelte degli elettori simulati",
    )

    parser.add_argument(
        "--porta",
        type=int,
        default=8000,
        help="porta del server locale (default 8000)",
    )

    parser.add_argument(
        "--registro",
        help="file in cui scrivere il registro pubblico alla chiusura",
    )

    return parser


def check_registry_path(text: str) -> Path:
    """
    Controlla all'avvio che il file del registro si possa scrivere,
    così l'errore compare subito e non alla chiusura dell'elezione.
    """

    path = Path(text)

    if path.is_dir():
        raise ValueError(f"{path} è una cartella, non un file.")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ValueError(
            f"Non riesco a creare la cartella {path.parent}: "
            f"{error.strerror or error}."
        ) from error

    writable = os.access(path, os.W_OK) if path.exists() else os.access(
        path.parent,
        os.W_OK,
    )

    if not writable:
        raise ValueError(f"Non ho i permessi per scrivere {path}.")

    return path


def main(argv: list[str] | None = None) -> None:
    """
    Prepara la prima simulazione e avvia il server.
    """

    parser = build_parser()
    arguments = parser.parse_args(argv)

    settings = SimulationSettings(
        mode=GROUP_MODES[arguments.gruppo],
        voters_per_district=arguments.elettori,
        guardian_count=arguments.garanti,
        quorum=arguments.quorum,
        seed=arguments.seme,
    )

    try:
        settings.validate()
        registry_path = (
            check_registry_path(arguments.registro)
            if arguments.registro
            else None
        )
        config = load_election_config(arguments.config)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    mode = MODES[settings.mode]
    host = DemoHost(config)

    print(f"\n{config.name}")
    print(
        f"Modalità {mode.label.lower()}: p da {mode.params.p.bit_length()} bit. "
        f"Cerimonia con {settings.guardian_count} garanti, quorum {settings.quorum}."
    )

    def progress(done: int, total: int) -> None:
        if done == total or done % 10 == 0:
            print(f"  elettori simulati: {done}/{total}", flush=True)

    start = time.perf_counter()
    # La prima simulazione va ancora scelta dalla pagina «Prepara».
    host.prepare(settings, background=False, progress=progress, confirm=False)

    if host.status.error is not None:
        print(f"Preparazione non riuscita: {host.status.error}", file=sys.stderr)
        sys.exit(1)

    print(f"Pronto in {time.perf_counter() - start:.1f} s.")
    print(
        f"Apri http://127.0.0.1:{arguments.porta} nel browser e comincia da «Prepara» "
        "(Ctrl+C per uscire).\n"
    )

    app = create_app(host, registry_path=registry_path)

    app.run(
        host="127.0.0.1",
        port=arguments.porta,
        debug=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
