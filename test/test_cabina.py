"""
Test della cabina elettorale dimostrativa.

La prima parte prova la logica di ElectionSession senza il server;
la seconda percorre le pagine con il client di test di Flask, come
farebbe un elettore nel browser. Si usa il gruppo didattico.
"""

import json
from pathlib import Path
import re

import pytest

from cabina.app import (
    create_app,
    parse_choice,
)
from cabina.sessione import (
    CLOSED,
    TAMPERINGS,
    ElectionSession,
    demo_codes,
    explain_violation,
    format_code,
    normalize_code,
    symbol_of,
)
from evoto.configurazione import load_election_config
from evoto.gruppo import (
    DEMO_PARAMS,
    TEST_PARAMS,
)
from evoto.scheda import BallotLayout
from evoto.urna import (
    CAST,
    SPOILED,
)
from evoto.voto import VoterChoice
from werkzeug.datastructures import MultiDict


CONFIG = load_election_config(
    Path(__file__).resolve().parent.parent
    / "config"
    / "elezione_esempio.json"
)


@pytest.fixture
def election() -> ElectionSession:
    """
    Elezione con due elettori simulati per circoscrizione.
    """

    session = ElectionSession(
        config=CONFIG,
        params=TEST_PARAMS,
        guardian_count=5,
        quorum=3,
    )

    session.prefill(voters_per_district=2, seed=1)

    return session


def test_demo_codes_per_district():
    """
    Dieci codici per circoscrizione, con il nome della circoscrizione.
    """

    codes = demo_codes(CONFIG, 10)

    assert len(codes) == 30
    assert codes["NORD-01"] == 0
    assert codes["CENTRO-10"] == 1
    assert codes["SUD-05"] == 2


def test_formatting_helpers():
    """
    I codici hanno la lunghezza di q in esadecimale, a gruppi di quattro.

    Nel gruppo didattico q = 1289 ha 11 bit, quindi 3 cifre:
    1288 = 0x508.
    """

    assert format_code(1288, TEST_PARAMS) == "508"
    assert format_code(5, TEST_PARAMS) == "005"

    full = format_code(DEMO_PARAMS.q - 1, DEMO_PARAMS)

    assert len(full.replace(" ", "")) == 64
    assert len(format_code(DEMO_PARAMS.q - 1, DEMO_PARAMS, short=True)) == 9

    assert normalize_code("ab12-cd 34") == "AB12CD34"
    assert symbol_of("Lista A") == "A"


def test_prefill_casts_one_ballot_per_simulated_voter(election):
    """
    Ogni elettore simulato deposita una scheda; la catena è valida e
    i codici dimostrativi sono ancora tutti disponibili.
    """

    counts = election.counts()

    assert counts["cast"] == 6
    assert counts["demo_voted"] == 0
    assert election.board_is_valid()
    assert len(election.available_demo_codes()) == 30

    with pytest.raises(ValueError):
        election.prefill(voters_per_district=2, seed=1)


def test_ballot_view_groups_coalitions_first(election):
    """
    Nella circoscrizione Nord: Coalizione Alfa (A, B, D), Coalizione
    Beta (E, F, G), poi le liste non coalizzate (C, H). La Lista B ha
    capolista Rinaldi C. e le caselle 4-7.
    """

    groups = election.ballot_view(0)

    assert [group.title for group in groups] == [
        "Coalizione Alfa",
        "Coalizione Beta",
        None,
    ]

    assert [item.symbol for item in groups[0].lists] == ["A", "B", "D"]
    assert [item.symbol for item in groups[2].lists] == ["C", "H"]

    list_b = groups[0].lists[1]

    assert list_b.head == "Rinaldi C."
    assert [candidate.box for candidate in list_b.candidates] == [4, 5, 6, 7]
    assert list_b.candidates[0].name == "Bruno E."


def test_cast_gives_a_tracking_code(election):
    """
    Un elettore dimostrativo vota la Lista B con due preferenze:
    la scheda entra in bacheca e il codice si ritrova con le sue
    prime cifre. Non si può votare due volte.
    """

    pending = election.prepare("NORD-01", VoterChoice(list_index=1, preferences=(4, 5)))
    entry = election.cast(pending.token)

    assert entry.state == CAST
    assert entry.district_index == 0
    assert election.counts()["demo_voted"] == 1

    found = election.find_entries(format_code(entry.tracking_code, TEST_PARAMS))

    assert entry in found

    # Un secondo deposito con lo stesso token (doppio clic, pagina
    # ricaricata) restituisce la stessa ricevuta senza votare di nuovo.
    assert election.cast(pending.token) == entry
    assert election.receipt(pending.token) == entry
    assert len(election.board.entries) == entry.sequence

    with pytest.raises(ValueError):
        election.prepare("NORD-01", VoterChoice(list_index=0))

    with pytest.raises(ValueError):
        election.receipt("sconosciuto")


def test_spoil_reveals_the_choice_and_allows_a_new_vote(election):
    """
    Sfida di Benaloh: la scheda sprecata contiene la scelta fatta,
    si ricifra, non conta, e l'elettore vota di nuovo.
    """

    choice = VoterChoice(list_index=1, preferences=(4, 5))
    token = election.prepare("NORD-02", choice).token
    report = election.spoil(token)

    assert report.entry.state == SPOILED
    assert report.voter_id == "NORD-02"
    assert report.matches_choice
    assert report.reencrypts
    assert report.decoded == "Lista B; preferenze: Bruno E., Costa D."

    # La stessa sfida ripetuta non pubblica una seconda scheda.
    assert election.spoil(token) == report
    assert election.spoil_report(token) == report

    entry = election.cast(election.prepare("NORD-02", choice).token)

    assert entry.sequence == report.entry.sequence + 1
    assert election.board_is_valid()


def test_spoil_after_voting_is_rejected(election):
    """
    Una scheda preparata ma non usata non si può sfidare dopo che
    l'elettore ha già depositato un'altra scheda.
    """

    old = election.prepare("NORD-03", VoterChoice(list_index=0))
    election.cast(election.prepare("NORD-03", VoterChoice(list_index=0)).token)

    with pytest.raises(ValueError, match="ha già votato"):
        election.spoil(old.token)


def test_invalid_choice_cannot_be_proved(election):
    """
    Un dispositivo scorretto non riesce a costruire le prove:
    preferenza in un'altra lista (R3) o quattro preferenze (R4).
    Nessuno dei due elettori risulta aver votato.
    """

    cases = (
        (VoterChoice(list_index=1, preferences=(0,)), "R3"),
        (VoterChoice(list_index=1, preferences=(4, 5, 6, 7)), "R4"),
    )

    for choice, rule in cases:
        with pytest.raises(ValueError, match=rule):
            election.prepare("NORD-03", choice)

    assert election.counts()["demo_voted"] == 0


def test_explain_violation():
    """
    Le spiegazioni seguono le regole della scheda. Nella circoscrizione
    Nord le caselle 4 e 6 della Lista B sono entrambe F; con un limite
    di una preferenza per genere la scelta viola R5.
    """

    session = ElectionSession(config=CONFIG, params=TEST_PARAMS)
    layout = session.layouts[0]

    def rule(target, list_index, preferences):
        reason = explain_violation(
            target,
            VoterChoice(list_index=list_index, preferences=preferences),
        )

        return None if reason is None else reason[:2]

    assert rule(layout, 1, (4, 5)) is None
    assert rule(layout, None, (4, 0)) == "R3"
    assert rule(layout, 1, (4, 5, 6, 7)) == "R4"

    strict = BallotLayout(
        list_count=layout.list_count,
        preference_metadata=layout.preference_metadata,
        max_preferences=3,
        max_preferences_per_gender=1,
    )

    assert rule(strict, 1, (4, 6)) == "R5"


def test_close_requires_the_quorum(election):
    """
    Con due garanti su cinque la decifratura è impossibile e
    l'elezione resta aperta; con 1, 3 e 5 si chiude.
    """

    with pytest.raises(ValueError):
        election.close((2, 4))

    with pytest.raises(ValueError):
        election.close((1, 3, 9))

    assert election.phase != CLOSED

    election.close((5, 3, 1))

    assert election.phase == CLOSED
    assert election.present_guardians == (1, 3, 5)
    assert sum(result.ballot_count for result in election.results) == 6
    assert sum(election.scrutiny.list_seats) == CONFIG.seats

    with pytest.raises(ValueError):
        election.prepare("NORD-04", VoterChoice(list_index=0))


def test_registry_is_verified_and_tampering_is_detected(election):
    """
    Il registro della cabina supera V1-V8; ogni manomissione
    dimostrativa fa fallire i controlli attesi.
    """

    election.cast(election.prepare("SUD-01", VoterChoice(list_index=4)).token)
    election.close((1, 3, 5))

    clean = election.verify()

    assert clean.overall
    assert all(clean.checks.values())
    assert election.verify() is clean

    expected = {
        "voto": {"V7", "V8"},
        "seggi": {"V8"},
        "scheda": {"V3", "V4", "V5"},
        "codice": {"V4"},
        "share": {"V6", "V7"},
        "garante": {"V2"},
    }

    assert set(expected) == set(TAMPERINGS)

    for tampering, failing in expected.items():
        report = election.verify(tampering)

        assert not report.overall
        assert {name for name, ok in report.checks.items() if not ok} == failing

    with pytest.raises(ValueError):
        election.registry_json("sconosciuta")


def test_registry_needs_a_closed_election(election):
    """
    Prima della chiusura non c'è un registro da pubblicare.
    """

    with pytest.raises(ValueError):
        election.registry_json()


def test_parse_choice():
    """
    Il modulo della scheda: lista, scheda bianca, preferenze.
    """

    assert parse_choice(MultiDict({"lista": "1", "preferenze": ["5", "4"]})) == VoterChoice(
        list_index=1,
        preferences=(4, 5),
    )

    assert parse_choice(MultiDict({"lista": "bianca"})) == VoterChoice(list_index=None)

    assert parse_choice(MultiDict({"preferenze": ["4"]})) == VoterChoice(
        list_index=None,
        preferences=(4,),
    )

    for form in (
        MultiDict(),
        MultiDict({"lista": "bianca", "preferenze": ["4"]}),
        MultiDict({"lista": "x"}),
    ):
        with pytest.raises(ValueError):
            parse_choice(form)


def test_web_flow(election, tmp_path):
    """
    Il percorso della demo all'esame: conferma della simulazione già
    pronta, accesso, scheda, sfida, nuovo voto, ricevuta, bacheca,
    chiusura con 3 garanti su 5, risultati, verifica, registro manomesso.
    """

    registry_path = tmp_path / "registro.json"
    client = create_app(election, registry_path=registry_path).test_client()

    # Si comincia da «Prepara»: le pagine del voto rimandano lì finché
    # la simulazione non è stata confermata.
    for page in ("/cabina", "/bacheca", "/spoglio", "/verifica"):
        assert client.get(page).headers["Location"].endswith("/prepara")

    response = client.post("/prepara", data={"azione": "usa"})

    assert response.headers["Location"].endswith("/cabina")

    for page in ("/", "/prepara", "/cabina", "/bacheca", "/spoglio", "/verifica"):
        assert client.get(page).status_code == 200

    # I risultati ora sono nella pagina dello spoglio.
    assert client.get("/risultati").headers["Location"].endswith("/spoglio")

    response = client.post("/cabina", data={"codice": "nord-05"})

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/cabina/NORD-05")

    page = client.get("/cabina/NORD-05").get_data(as_text=True)

    assert "Rinaldi C." in page and "Bruno E." in page

    page = client.post(
        "/cabina/NORD-05",
        data={"lista": "1", "preferenze": ["4"]},
    ).get_data(as_text=True)

    token = re.search(r"/scheda/([^/]+)/sfida", page).group(1)
    response = client.post(f"/scheda/{token}/sfida")

    assert response.headers["Location"].endswith(f"/sfida/{token}")

    page = client.get(f"/sfida/{token}").get_data(as_text=True)

    assert "Lista B; preferenze: Bruno E." in page
    assert "Vota di nuovo come NORD-05" in page

    page = client.post(
        "/cabina/NORD-05",
        data={"lista": "1", "preferenze": ["4"]},
    ).get_data(as_text=True)

    token = re.search(r"/scheda/([^/]+)/deposita", page).group(1)
    response = client.post(f"/scheda/{token}/deposita")

    assert response.headers["Location"].endswith(f"/ricevuta/{token}")

    # Doppio clic sul deposito: stessa ricevuta, nessuna scheda in più.
    entries = len(election.board.entries)
    client.post(f"/scheda/{token}/deposita")

    assert len(election.board.entries) == entries

    page = client.get(f"/ricevuta/{token}").get_data(as_text=True)

    assert "Scheda depositata" in page

    entry = election.board.entries[-1]
    short = format_code(entry.tracking_code, TEST_PARAMS, short=True)
    page = client.get("/bacheca", query_string={"codice": short}).get_data(as_text=True)

    assert f'la scheda è in posizione\n      <a href="#riga-{entry.sequence}">' in page

    # Un codice copiato con i puntini di sospensione si trova lo stesso.
    page = client.get("/bacheca", query_string={"codice": short + "…"}).get_data(as_text=True)

    assert f"#riga-{entry.sequence}" in page

    page = client.get("/bacheca", query_string={"codice": "1"}).get_data(as_text=True)

    assert "Scrivi almeno 3 cifre del codice" in page

    page = client.post(
        "/cabina/NORD-06",
        data={"lista": "1", "preferenze": ["0"], "scorretto": "1"},
    ).get_data(as_text=True)

    assert "R3" in page

    # La scheda torna con le scelte fatte e il dispositivo scorretto attivo.
    assert re.search(r'name="lista" value="1"\s+checked', page)
    assert re.search(r'value="0"\s+data-list="0" data-gender="M"\s+checked', page)
    assert re.search(r'id="scorretto" name="scorretto" value="1" checked', page)

    page = client.post(
        "/spoglio",
        data={"garanti": ["1", "2"], "conferma": "1"},
    ).get_data(as_text=True)

    assert "Servono almeno 3 garanti" in page

    page = client.post("/spoglio", data={"garanti": ["1", "3", "5"]}).get_data(as_text=True)

    assert "Spunta la conferma" in page
    assert election.phase != CLOSED

    response = client.post(
        "/spoglio",
        data={"garanti": ["1", "3", "5"], "conferma": "1"},
    )

    assert response.status_code == 302
    assert registry_path.exists()

    page = client.get("/spoglio").get_data(as_text=True)

    assert "assenti: 2, 4" in page
    assert "Verifica l'elezione</a>" in page

    page = client.post("/verifica", data={"manomissione": ""}).get_data(as_text=True)

    assert "ELEZIONE VERIFICATA" in page

    page = client.post("/verifica", data={"manomissione": "voto"}).get_data(as_text=True)

    assert "VERIFICA FALLITA" in page

    response = client.get("/registro.json", query_string={"manomissione": "seggi"})

    assert response.status_code == 200
    assert "registro_manomesso_seggi.json" in response.headers["Content-Disposition"]
    assert json.loads(response.get_data(as_text=True))["scrutiny"]

    assert json.loads(registry_path.read_text(encoding="utf-8")) == json.loads(
        client.get("/registro.json").get_data(as_text=True)
    )

    assert client.get("/cabina/NORD-07").status_code == 400


def test_demo_group_end_to_end():
    """
    La cabina con il gruppo da 2048 bit, che è quello di default della
    demo: un elettore simulato per circoscrizione, chiusura con tre
    garanti su cinque, registro verificato.
    """

    session = ElectionSession(
        config=CONFIG,
        params=DEMO_PARAMS,
        guardian_count=5,
        quorum=3,
    )

    session.prefill(voters_per_district=1, seed=3)
    session.close((2, 4, 5))

    assert sum(result.ballot_count for result in session.results) == 3
    assert session.verify().overall
