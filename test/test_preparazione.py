"""
Test della preparazione delle simulazioni e del sito guidato.

La prima parte prova preparazione.py senza il server: scelte della
pagina «Prepara», tendenze di voto, DemoHost. La seconda percorre le
pagine nuove con il client di test di Flask: preparazione dal browser,
menu a passi, registro non scrivibile, riga di comando. Si usa la
modalità veloce (gruppo didattico).
"""

from dataclasses import replace
from pathlib import Path
import threading
import time

import pytest
from werkzeug.datastructures import MultiDict

from cabina.__main__ import (
    check_registry_path,
    main,
)
import cabina.preparazione as preparazione
from cabina.app import (
    create_app,
    navigation,
    next_step,
)
from cabina.preparazione import (
    MODES,
    DemoHost,
    PreparationStatus,
    SimulationSettings,
    format_duration,
    mode_of,
    orientation_weights,
    parse_settings,
    settings_to_form,
)
from cabina.sessione import ElectionSession
from evoto.configurazione import load_election_config
from evoto.gruppo import (
    DEMO_PARAMS,
    TEST_PARAMS,
)
from evoto.voto import VoterChoice


CONFIG = load_election_config(
    Path(__file__).resolve().parent.parent
    / "config"
    / "elezione_esempio.json"
)

FAST = SimulationSettings(
    mode="veloce",
    voters_per_district=4,
    spoil_percent=0,
)


def wait_until_ready(host: DemoHost, timeout: float = 30.0) -> None:
    """
    Aspetta che la preparazione in background finisca.
    """

    deadline = time.monotonic() + timeout

    while host.preparing:
        assert time.monotonic() < deadline, "preparazione troppo lenta"
        time.sleep(0.02)


def test_default_settings_are_valid():
    """
    Le scelte di default: modalità realistica, 10 elettori per
    circoscrizione, 5 garanti con quorum 3.
    """

    settings = SimulationSettings()
    settings.validate()

    assert settings.mode == "realistica"
    assert parse_settings(settings_to_form(settings)) == settings


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"mode": "lenta"}, "modalità"),
        ({"voters_per_district": 21}, "da 0 a 20"),
        ({"voters_per_district": -1}, "da 0 a 20"),
        ({"orientation": "a caso"}, "tendenza"),
        ({"blank_percent": 21}, "bianche"),
        ({"spoil_percent": 31}, "sfide"),
        ({"guardian_count": 3, "quorum": 4}, "quorum"),
        ({"guardian_count": 10, "quorum": 3}, "garanti"),
        ({"seed": -1}, "seme"),
    ],
)
def test_invalid_settings(changes, message):
    """
    Ogni scelta fuori dai limiti viene rifiutata con un messaggio da
    mostrare nella pagina.
    """

    with pytest.raises(ValueError, match=message):
        replace(SimulationSettings(), **changes).validate()


def test_fast_mode_allows_more_voters():
    """
    In modalità veloce si arriva a 200 elettori per circoscrizione.
    """

    SimulationSettings(mode="veloce", voters_per_district=200).validate()

    with pytest.raises(ValueError, match="da 0 a 200"):
        SimulationSettings(mode="veloce", voters_per_district=201).validate()


def test_parse_settings_from_the_form():
    """
    Il modulo arriva come testo: numeri interi, garanti nella forma
    «n-quorum», seme vuoto uguale al default.
    """

    settings = parse_settings(
        MultiDict(
            {
                "modalita": "veloce",
                "elettori": "50",
                "tendenza": "testa_a_testa",
                "bianche": "0",
                "sfide": "10",
                "garanti": "7-4",
                "seme": "",
            }
        )
    )

    assert settings == SimulationSettings(
        mode="veloce",
        voters_per_district=50,
        orientation="testa_a_testa",
        blank_percent=0,
        spoil_percent=10,
        guardian_count=7,
        quorum=4,
        seed=2026,
    )

    for form in ({"elettori": "dieci"}, {"garanti": "5"}, {"garanti": "a-b"}):
        with pytest.raises(ValueError, match="numeri interi"):
            parse_settings(MultiDict(form))


def test_orientation_weights():
    """
    Nell'elezione di esempio la Coalizione Alfa ha le liste A, B, D e
    la Coalizione Beta le liste E, F, G; C e H sono da sole.
    """

    alfa = (0, 1, 3)
    beta = (4, 5, 6)

    def share(weights, indices):
        return sum(weights[index] for index in indices) / sum(weights)

    in_testa = orientation_weights(CONFIG, "in_testa", 1)

    assert share(in_testa, alfa) == pytest.approx(0.47)
    assert in_testa[0] == in_testa[1] == in_testa[3]

    close = orientation_weights(CONFIG, "testa_a_testa", 1)

    assert share(close, alfa) == pytest.approx(0.37)
    assert share(close, beta) == pytest.approx(0.37)

    assert set(orientation_weights(CONFIG, "equilibrato", 1)) == {1.0}

    surprise = orientation_weights(CONFIG, "casuale", 5)

    assert surprise == orientation_weights(CONFIG, "casuale", 5)
    assert surprise != orientation_weights(CONFIG, "casuale", 6)
    assert all(0.5 <= weight <= 3.0 for weight in surprise)

    with pytest.raises(ValueError):
        orientation_weights(CONFIG, "sconosciuta", 1)


def test_orientation_weights_without_coalitions():
    """
    Senza coalizioni le prime liste fanno da gruppi: in testa c'è la
    prima lista, nel testa a testa le prime due.
    """

    config = replace(CONFIG, coalitions=())

    in_testa = orientation_weights(config, "in_testa", 1)
    close = orientation_weights(config, "testa_a_testa", 1)

    assert in_testa[0] == pytest.approx(0.47)
    assert close[0] == close[1] == pytest.approx(0.37)


def test_format_duration():
    """
    Le durate mostrate nella pagina sono arrotondate.
    """

    assert format_duration(0.3) == "meno di un secondo"
    assert format_duration(1.2) == "circa un secondo"
    assert format_duration(28.4) == "circa 28 secondi"
    assert format_duration(75) == "circa un minuto"
    assert format_duration(150) == "circa 2 minuti"


def test_remaining_seconds():
    """
    Il tempo che manca si stima dalla velocità misurata, oppure dalla
    stima iniziale finché nessun elettore ha votato.
    """

    voting = PreparationStatus(phase="voto", done=5, total=20, started=100.0)

    assert voting.remaining_seconds(110.0) == pytest.approx(30.0)

    ceremony = PreparationStatus(phase="cerimonia", total=20, started=100.0, estimate=12.0)

    assert ceremony.remaining_seconds(104.0) == pytest.approx(8.0)
    assert ceremony.remaining_seconds(200.0) == 0.0
    assert PreparationStatus().remaining_seconds(0.0) is None


def test_mode_of_params():
    """
    Ogni gruppo corrisponde a una modalità.
    """

    assert mode_of(DEMO_PARAMS) is MODES["realistica"]
    assert mode_of(TEST_PARAMS) is MODES["veloce"]
    assert mode_of(replace(TEST_PARAMS, g=TEST_PARAMS.g ** 2 % TEST_PARAMS.p)) is None


def test_host_prepares_in_the_foreground():
    """
    Con background=False la preparazione avviene subito: dodici schede
    depositate, velocità misurata, scelte ricordate.
    """

    host = DemoHost(CONFIG)
    steps = []

    assert host.session is None
    assert host.estimate_verification_seconds() is None

    host.prepare(FAST, background=False, progress=lambda done, total: steps.append(done))

    assert host.status == PreparationStatus()
    assert host.settings == FAST
    assert host.mode is MODES["veloce"]
    assert host.session.counts()["cast"] == 12
    assert host.session.guardian_count == 5
    assert steps == list(range(1, 13))

    # La stima usa la velocità appena misurata.
    measured = host.seconds_per_ballot("veloce")

    assert measured == host.session.seconds_per_ballot
    assert host.estimate_seconds(replace(FAST, voters_per_district=10)) == pytest.approx(
        30 * measured
    )
    assert host.estimate_seconds(SimulationSettings()) == pytest.approx(
        30 * 1.05 * MODES["realistica"].seconds_per_ballot
    )


def test_failed_preparation_keeps_the_previous_election(monkeypatch):
    """
    Se la preparazione fallisce, l'errore resta nello stato e la
    simulazione precedente continua a funzionare.
    """

    host = DemoHost(CONFIG)
    host.prepare(FAST, background=False)
    previous = host.session

    class BrokenSession(ElectionSession):
        def prefill(self, *args, **kwargs):
            raise ValueError("guasto simulato")

    monkeypatch.setattr(preparazione, "ElectionSession", BrokenSession)

    host.prepare(replace(FAST, seed=9), background=False)

    assert host.status.phase == "errore"
    assert host.status.error == "guasto simulato"
    assert host.session is previous
    assert host.settings == FAST


def test_background_preparation_and_progress_page(monkeypatch):
    """
    Mentre una simulazione si prepara in background: una seconda
    preparazione è rifiutata, le pagine dell'elezione rimandano alla
    pagina di avanzamento e /stato riporta il progresso. Alla fine la
    nuova elezione sostituisce la precedente.
    """

    release = threading.Event()

    class SlowSession(ElectionSession):
        def prefill(self, *args, **kwargs):
            assert release.wait(10)
            super().prefill(*args, **kwargs)

    host = DemoHost(CONFIG)
    host.prepare(FAST, background=False)
    previous = host.session

    monkeypatch.setattr(preparazione, "ElectionSession", SlowSession)

    client = create_app(host).test_client()
    host.prepare(replace(FAST, seed=3))

    try:
        assert host.preparing

        with pytest.raises(ValueError, match="già in preparazione"):
            host.prepare(FAST)

        assert client.get("/cabina").headers["Location"].endswith("/prepara")
        assert client.get("/").status_code == 200

        page = client.get("/prepara").get_data(as_text=True)

        assert "Preparazione in corso" in page
        assert 'class="step locked"' in page

        status = client.get("/stato").get_json()

        assert status["running"] is True
        assert status["total"] == 12
    finally:
        release.set()

    wait_until_ready(host)

    assert host.session is not previous
    assert host.settings.seed == 3
    assert client.get("/stato").get_json()["running"] is False


def test_navigation_follows_the_election():
    """
    Il menu a passi e l'azione consigliata nella panoramica seguono lo
    stato dell'elezione.
    """

    host = DemoHost(CONFIG)

    assert next_step(host) == ("Prepara una simulazione", "prepare")
    assert [step.locked for step in navigation(host)] == [False, True, True, True, True]

    host.prepare(FAST, background=False)
    session = host.session

    def states():
        return {
            step.key: ("fatto" if step.done else "") + ("bloccato" if step.locked else "")
            for step in navigation(host)
        }

    assert states() == {
        "prepara": "fatto",
        "vota": "",
        "bacheca": "",
        "spoglio": "",
        "verifica": "bloccato",
    }
    assert next_step(host)[1] == "access"

    card = next(iter(session.demo_voters))
    session.cast(session.prepare(card, VoterChoice(list_index=0)).token)

    assert states()["vota"] == "fatto"
    assert next_step(host)[1] == "closing"

    session.close((1, 3, 5))

    assert states() == {
        "prepara": "fatto",
        "vota": "fatto",
        "bacheca": "fatto",
        "spoglio": "fatto",
        "verifica": "",
    }
    assert next_step(host)[1] == "verification"

    session.verify()

    assert states()["verifica"] == "fatto"
    assert next_step(host)[1] == "prepare"


def test_startup_simulation_must_be_chosen_in_prepare():
    """
    La simulazione preparata all'avvio non porta direttamente al voto:
    la panoramica indica «Prepara», le pagine del voto rimandano lì e il
    menu le blocca. Con «Usa questa simulazione» la stessa elezione,
    senza prepararne un'altra, passa al voto.
    """

    host = DemoHost(CONFIG)
    host.prepare(FAST, background=False, confirm=False)
    session = host.session
    client = create_app(host).test_client()

    assert not host.confirmed
    assert not host.ready
    assert next_step(host) == ("Inizia: prepara la simulazione", "prepare")
    assert [(step.done, step.locked) for step in navigation(host)] == [
        (False, False),
        (False, True),
        (False, True),
        (False, True),
        (False, True),
    ]

    page = client.get("/").get_data(as_text=True)

    assert 'href="/prepara">Inizia: prepara la simulazione' in page
    assert "Si comincia da Prepara" in page

    card = next(iter(session.demo_voters))

    for path in ("/cabina", f"/cabina/{card}", "/bacheca", "/spoglio"):
        assert client.get(path).headers["Location"].endswith("/prepara")

    page = client.get("/prepara").get_data(as_text=True)

    assert "Una simulazione è già pronta" in page
    assert "4 per circoscrizione" in page
    assert "Usa questa simulazione" in page

    response = client.post("/prepara", data={"azione": "usa"})

    assert response.headers["Location"].endswith("/cabina")
    assert host.confirmed and host.ready
    assert host.session is session
    assert navigation(host)[0].done
    assert next_step(host) == ("Vai a votare", "access")
    assert client.get("/cabina").status_code == 200
    assert "Una simulazione è già pronta" not in client.get("/prepara").get_data(as_text=True)


def test_a_new_preparation_counts_as_chosen():
    """
    Preparare dal browser una nuova simulazione vale come scelta, anche
    se quella dell'avvio non era stata confermata.
    """

    host = DemoHost(CONFIG)
    host.prepare(FAST, background=False, confirm=False)
    host.prepare(replace(FAST, seed=5), background=False)

    assert host.confirmed
    assert host.settings.seed == 5


def test_confirm_needs_a_simulation():
    """
    Senza una simulazione pronta non c'è niente da confermare: la pagina
    mostra il messaggio e le pagine del voto restano bloccate.
    """

    host = DemoHost(CONFIG)

    with pytest.raises(ValueError, match="Non c'è ancora una simulazione"):
        host.confirm()

    client = create_app(host).test_client()
    page = client.post("/prepara", data={"azione": "usa"}).get_data(as_text=True)

    assert "Non c&#39;è ancora una simulazione" in page
    assert not host.confirmed
    assert DemoHost(CONFIG, confirmed=True).confirmed is False


def test_command_line_starts_from_prepare(monkeypatch):
    """
    La riga di comando prepara la prima simulazione ma non la conferma:
    nel browser si comincia comunque da «Prepara».
    """

    started = {}

    class FakeApp:
        def run(self, **options):
            started.update(options)

    def fake_create_app(host, registry_path=None):
        started["demo"] = host
        return FakeApp()

    monkeypatch.setattr("cabina.__main__.create_app", fake_create_app)

    main(["--gruppo", "didattico", "--elettori", "1", "--porta", "8123"])

    host = started["demo"]

    assert started["port"] == 8123
    assert started["host"] == "127.0.0.1"
    assert host.session.counts()["cast"] == 3
    assert host.mode is MODES["veloce"]
    assert not host.confirmed


def test_prepare_page_from_the_browser():
    """
    Dalla pagina «Prepara»: un valore fuori dai limiti torna con il
    messaggio e con i valori scritti; una scelta valida prepara la
    nuova simulazione e la pagina annuncia che è pronta.
    """

    host = DemoHost(CONFIG)
    host.prepare(FAST, background=False)
    client = create_app(host).test_client()

    page = client.get("/prepara").get_data(as_text=True)

    assert 'value="veloce"\n                   checked' in page
    assert "Liste da sole</strong>: Lista C, Lista H" in page

    form = {
        "modalita": "realistica",
        "elettori": "500",
        "tendenza": "equilibrato",
        "bianche": "0",
        "sfide": "0",
        "garanti": "3-2",
        "seme": "11",
    }

    page = client.post("/prepara", data=form).get_data(as_text=True)

    assert "vanno da 0 a 20" in page
    assert 'value="500"' in page
    assert host.settings == FAST

    form.update(modalita="veloce", elettori="3")
    response = client.post("/prepara", data=form)

    assert response.headers["Location"].endswith("/prepara?pronta=1")

    wait_until_ready(host)

    assert host.settings == SimulationSettings(
        mode="veloce",
        voters_per_district=3,
        orientation="equilibrato",
        blank_percent=0,
        spoil_percent=0,
        guardian_count=3,
        quorum=2,
        seed=11,
    )
    assert host.session.counts()["cast"] == 9

    page = client.get("/prepara?pronta=1").get_data(as_text=True)

    assert "La simulazione è pronta: 9 schede depositate" in page


def test_registry_that_cannot_be_written(tmp_path):
    """
    Se il file del registro non si può scrivere, l'elezione si chiude
    lo stesso e le pagine lo segnalano; il registro resta scaricabile.
    """

    host = DemoHost(CONFIG)
    host.prepare(FAST, background=False)

    # Il percorso indicato è una cartella: la scrittura fallisce.
    client = create_app(host, registry_path=tmp_path).test_client()

    response = client.post("/spoglio", data={"garanti": ["1", "3", "5"], "conferma": "1"})

    assert response.status_code == 302
    assert host.registry_warning is not None

    for page in ("/spoglio", "/verifica"):
        assert "Il registro non è stato scritto" in client.get(page).get_data(as_text=True)

    assert client.get("/registro.json").status_code == 200


def test_unknown_receipt_and_challenge(tmp_path):
    """
    Ricevute e sfide inesistenti: pagina d'errore 404.
    """

    host = DemoHost(CONFIG)
    host.prepare(FAST, background=False)
    client = create_app(host).test_client()

    assert client.get("/ricevuta/nessuno").status_code == 404
    assert client.get("/sfida/nessuno").status_code == 404


def test_closing_without_votes_keeps_the_election_open():
    """
    Con zero elettori simulati e nessun voto lo spoglio non ha voti
    validi: la pagina lo dice e l'elezione resta aperta.
    """

    host = DemoHost(CONFIG)
    host.prepare(replace(FAST, voters_per_district=0), background=False)
    client = create_app(host).test_client()

    page = client.post(
        "/spoglio",
        data={"garanti": ["1", "3", "5"], "conferma": "1"},
    ).get_data(as_text=True)

    assert "Non ci sono voti validi" in page
    assert host.session.phase != "chiusa"


def test_check_registry_path(tmp_path):
    """
    All'avvio il percorso del registro deve essere un file scrivibile.
    """

    path = tmp_path / "nuova" / "registro.json"

    assert check_registry_path(str(path)) == path
    assert path.parent.is_dir()

    with pytest.raises(ValueError, match="cartella"):
        check_registry_path(str(tmp_path))


@pytest.mark.parametrize(
    "argv",
    [
        ["--elettori", "50"],
        ["--gruppo", "didattico", "--elettori", "500"],
        ["--garanti", "3", "--quorum", "4"],
        ["--config", "manca.json"],
    ],
)
def test_command_line_rejects_invalid_options(argv, capsys):
    """
    Opzioni non valide: argparse esce con il codice 2 e un messaggio,
    prima di preparare l'elezione.
    """

    with pytest.raises(SystemExit) as exit_info:
        main(argv)

    assert exit_info.value.code == 2
    assert "error" in capsys.readouterr().err
