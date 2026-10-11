"""
Test del verificatore da riga di comando (python -m verifica).
"""

import json
from pathlib import Path
import subprocess
import sys

from evoto.configurazione import load_election_config
from evoto.gruppo import TEST_PARAMS
from evoto.registro import public_registry_to_json
from evoto.simulazione import simulate_election
from verifica.__main__ import main


ROOT = Path(__file__).resolve().parent.parent

REGISTRY = public_registry_to_json(
    simulate_election(
        config=load_election_config(ROOT / "config" / "elezione_esempio.json"),
        voters_per_district=2,
        guardian_count=3,
        quorum=2,
        present_guardians=(1, 3),
        params=TEST_PARAMS,
        seed=4,
    ),
    TEST_PARAMS,
)


def test_verified_registry(tmp_path, capsys):
    """
    Un registro valido: tutti i controlli OK e codice di uscita 0.
    """

    path = tmp_path / "registro.json"
    path.write_text(REGISTRY, encoding="utf-8")

    assert main([str(path)]) == 0

    output = capsys.readouterr().out

    assert "ELEZIONE VERIFICATA" in output
    assert output.count("OK  ") == 8


def test_tampered_registry(tmp_path, capsys):
    """
    Un voto in più: falliscono V7 e V8, codice di uscita 1.
    """

    data = json.loads(REGISTRY)
    data["district_results"][0]["list_votes"][0] += 1

    path = tmp_path / "registro_manomesso.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    assert main([str(path)]) == 1

    output = capsys.readouterr().out

    assert "VERIFICA FALLITA" in output
    assert "FAIL  V7" in output
    assert "FAIL  V8" in output


def test_missing_file(tmp_path, capsys):
    """
    Un file che non esiste: codice di uscita 2 e messaggio d'errore.
    """

    assert main([str(tmp_path / "manca.json")]) == 2
    assert "Impossibile leggere" in capsys.readouterr().err


def test_runs_as_a_module_without_importing_evoto(tmp_path):
    """
    python -m verifica funziona da solo e non importa evoto.
    """

    path = tmp_path / "registro.json"
    path.write_text(REGISTRY, encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-m", "verifica", str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "ELEZIONE VERIFICATA" in completed.stdout

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, verifica.__main__; "
            "print(any(name.startswith('evoto') for name in sys.modules))",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert probe.stdout.strip() == "False"
