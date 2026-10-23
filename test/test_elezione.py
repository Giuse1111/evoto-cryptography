"""
Test d'integrazione: un'elezione politica completa (esperimento E1).

Usa la configurazione di esempio: tre circoscrizioni, sei liste in due
coalizioni e due liste singole. Gli elettori scelgono a caso, alcuni
sprecano una scheda per controllare il dispositivo.

Il risultato del percorso cifrato deve coincidere con un conteggio in
chiaro delle stesse scelte, fino ai seggi e agli eletti.
"""

from pathlib import Path
import random
import json
from copy import deepcopy

from evoto.configurazione import (
    build_ballot_layout,
    config_fingerprint,
    load_election_config,
)
from evoto.gruppo import (
    DEMO_PARAMS,
    TEST_PARAMS,
)
from evoto.scheda import verify_ballot
from evoto.simulazione import (
    plaintext_count,
    random_choice,
    simulate_election,
)
from evoto.urna import CAST, SPOILED, verify_spoiled_ballot
from evoto.voto import VoterChoice, choice_to_plaintexts
from evoto.registro import (
    build_public_registry,
    public_registry_to_json,
)
from verifica.verifica import (
    configuration_fingerprint,
    hash_to_q,
    recompute_v8_scrutiny,
    verify_public_registry,
    verify_v4_board_rules,
)


CONFIG = load_election_config(
    Path(__file__).resolve().parent.parent
    / "config"
    / "elezione_esempio.json"
)

WEIGHTS = (22, 14, 12, 9, 16, 11, 7, 6)

REPORT = simulate_election(
    config=CONFIG,
    voters_per_district=15,
    guardian_count=5,
    quorum=3,
    present_guardians=(2, 4, 5),
    params=TEST_PARAMS,
    seed=1,
    list_weights=WEIGHTS,
    spoil_probability=0.2,
)


def test_encrypted_totals_match_the_plaintext_count():
    """
    Per ogni circoscrizione liste, bianche e preferenze coincidono.
    """

    for cipher, clear in zip(
        REPORT.results,
        REPORT.plaintext_results,
        strict=True,
    ):
        assert cipher.ballot_count == clear.ballot_count == 15
        assert cipher.list_votes == clear.list_votes
        assert cipher.blank_votes == clear.blank_votes
        assert cipher.preference_votes == clear.preference_votes


def test_seats_and_elected_match_the_plaintext_count():
    """
    Anche seggi, premio ed eletti coincidono.
    """

    assert REPORT.scrutiny == REPORT.plaintext_scrutiny


def test_every_seat_is_assigned():
    """
    I 30 seggi sono tutti assegnati, a 30 eletti diversi.
    """

    scrutiny = REPORT.scrutiny

    assert sum(scrutiny.list_seats) == CONFIG.seats
    assert len(scrutiny.elected) == CONFIG.seats

    elected = {
        (candidate.district_index, candidate.name)
        for candidate in scrutiny.elected
    }

    assert len(elected) == CONFIG.seats


def test_board_is_consistent():
    """
    La catena è valida, ogni scheda ha prove valide e ogni scheda
    sprecata si riapre con i dati rivelati.
    """

    assert REPORT.board_is_valid
    assert REPORT.spoiled_count > 0

    layouts = tuple(
        build_ballot_layout(CONFIG, index)
        for index in range(len(CONFIG.districts))
    )

    public_key = REPORT.ceremony.joint_public_key

    for entry in REPORT.board.entries:
        assert verify_ballot(
            layout=layouts[entry.district_index],
            ballot=entry.ballot,
            proofs=entry.proofs,
            public_key=public_key,
            params=TEST_PARAMS,
            context=REPORT.board.extended_base_hash,
        )

        if entry.state == SPOILED:
            assert verify_spoiled_ballot(
                entry.ballot,
                entry.revealed_witness,
                public_key,
                TEST_PARAMS,
            )

    cast = [entry for entry in REPORT.board.entries if entry.state == CAST]

    assert len(cast) == 45
    assert len(REPORT.board.entries) == 45 + REPORT.spoiled_count


def test_random_choices_are_always_valid():
    """
    Le scelte casuali rispettano la forma della scheda: al massimo tre
    preferenze, tutte nella stessa lista. Possono essere tutte dello
    stesso genere: dopo l'annullamento fatto dal dispositivo le
    preferenze dei due generi differiscono al più di 1 (R5).
    """

    rng = random.Random(3)
    layout = build_ballot_layout(CONFIG, 1)
    annulled = 0

    for _ in range(300):
        choice = random_choice(rng, layout, WEIGHTS, 0.05, 3)

        assert len(choice.preferences) <= 3

        lists = {
            layout.preference_metadata[box].list_index
            for box in choice.preferences
        }

        assert len(lists) <= 1

        if choice.list_index is not None and lists:
            assert lists == {choice.list_index}

        _, _, bits = choice_to_plaintexts(layout, choice)

        genders = [
            layout.preference_metadata[box].gender
            for box, bit in enumerate(bits)
            if bit == 1
        ]

        assert abs(genders.count("F") - genders.count("M")) <= 1

        annulled += len(choice.preferences) - len(genders)

    # Nelle 300 scelte capitano anche preferenze dello stesso genere.
    assert annulled > 0


def test_plaintext_count():
    """
    Il conteggio in chiaro applica la stessa regola della scheda:
    una preferenza senza lista vale per la lista.
    """

    layout = build_ballot_layout(CONFIG, 0)

    result = plaintext_count(
        layout,
        0,
        [
            VoterChoice(list_index=1, preferences=(4,)),
            VoterChoice(list_index=None, preferences=(0, 1)),
            VoterChoice(list_index=None),
        ],
    )

    assert result.ballot_count == 3
    assert result.list_votes[:2] == (1, 1)
    assert result.blank_votes == 1
    assert result.preference_votes[:5] == (1, 1, 0, 0, 1)


def test_plaintext_count_applies_the_gender_rule():
    """
    Il conteggio in chiaro annulla le preferenze come lo scrutatore:
    nella Lista A le caselle 0 e 2 sono due candidati M, quindi conta
    solo la prima.
    """

    layout = build_ballot_layout(CONFIG, 0)

    assert layout.preference_metadata[0].gender == "M"
    assert layout.preference_metadata[2].gender == "M"

    result = plaintext_count(
        layout,
        0,
        [
            VoterChoice(list_index=0, preferences=(0, 2)),
            VoterChoice(list_index=0, preferences=(0, 1, 2)),
        ],
    )

    assert result.list_votes[0] == 2
    assert result.preference_votes[:4] == (2, 1, 1, 0)


def test_simulation_preserves_encrypted_tallies():
    """
    La simulazione conserva i tally cifrati
    destinati al registro pubblico.
    """

    assert len(REPORT.tallies) == len(
        CONFIG.districts
    )

    for district_index, tally in enumerate(
        REPORT.tallies
    ):
        assert (
            tally.district_index
            == district_index
        )

        assert tally.ballot_count == 15


def test_public_registry_contains_only_public_election_data():
    """
    Il registro contiene i dati pubblici necessari alla verifica.
    """

    registry = build_public_registry(
        REPORT,
        TEST_PARAMS,
    )

    assert registry["configuration"]["election_id"] == (
        CONFIG.election_id
    )

    assert registry["group"] == {
        "p": TEST_PARAMS.p,
        "q": TEST_PARAMS.q,
        "g": TEST_PARAMS.g,
    }

    # e è l'impronta della configurazione, non il suo election_id.
    assert registry["election_context"] == {
        "n": 5,
        "quorum": 3,
        "e": config_fingerprint(CONFIG, TEST_PARAMS),
        "Q": REPORT.ceremony.base_hash,
        "Q_bar": REPORT.ceremony.extended_base_hash,
    }

    assert registry["K"] == (
        REPORT.ceremony.joint_public_key
    )

    assert len(registry["guardians"]) == 5

    assert len(registry["district_tallies"]) == (
        len(CONFIG.districts)
    )

    assert len(registry["district_results"]) == (
        len(CONFIG.districts)
    )

    assert registry["bulletin_board"][
        "extended_base_hash"
    ] == REPORT.ceremony.extended_base_hash

    assert registry["scrutiny"]["list_seats"] == (
        list(REPORT.scrutiny.list_seats)
    )

    assert "secret_shares" not in registry
    assert "plaintext_results" not in registry
    assert "plaintext_scrutiny" not in registry
    assert "timings" not in registry


def test_public_registry_is_valid_json():
    """
    Il registro completo può essere serializzato e riletto come JSON.
    """

    encoded = public_registry_to_json(
        REPORT,
        TEST_PARAMS,
    )

    decoded = json.loads(encoded)

    assert decoded["group"]["p"] == TEST_PARAMS.p
    assert decoded["group"]["q"] == TEST_PARAMS.q
    assert decoded["group"]["g"] == TEST_PARAMS.g

    assert decoded["election_context"]["n"] == 5
    assert decoded["election_context"]["quorum"] == 3

    assert decoded["K"] == (
        REPORT.ceremony.joint_public_key
    )


def test_public_registry_never_reveals_cast_witnesses():
    """
    Nessuna scheda CAST pubblica plaintext o nonce.
    """

    registry = build_public_registry(
        REPORT,
        TEST_PARAMS,
    )

    for entry in registry["bulletin_board"]["entries"]:
        if entry["state"] == CAST:
            assert "revealed_witness" not in entry

        if entry["state"] == SPOILED:
            assert "revealed_witness" in entry


def test_independent_verifier_accepts_complete_registry():
    """
    Il verificatore indipendente accetta l'elezione completa.
    """

    serialized = public_registry_to_json(
        REPORT,
        TEST_PARAMS,
    )

    verification = (
        verify_public_registry(
            serialized
        )
    )

    assert verification == {
        "V1": True,
        "V2": True,
        "V3": True,
        "V4": True,
        "V5": True,
        "V6": True,
        "V7": True,
        "V8": True,
        "overall": True,
    }


def test_independent_verifier_rejects_tampered_registry():
    """
    Una modifica al registro rende negativa la verifica complessiva.
    """

    registry = build_public_registry(
        REPORT,
        TEST_PARAMS,
    )

    registry["district_tallies"][0][
        "list_tallies"
    ][0]["beta"] = 1

    serialized = json.dumps(
        registry
    )

    verification = (
        verify_public_registry(
            serialized
        )
    )

    assert not verification["V5"]
    assert not verification["overall"]


def test_independent_verifier_rejects_false_spoiled_witness():
    """
    Il verificatore rifiuta un witness SPOILED alterato.
    """

    registry = build_public_registry(
        REPORT,
        TEST_PARAMS,
    )

    spoiled_entry = next(
        entry
        for entry
        in registry[
            "bulletin_board"
        ]["entries"]
        if entry["state"] == SPOILED
    )

    spoiled_entry[
        "revealed_witness"
    ]["blank_nonce"] += 1

    assert not verify_v4_board_rules(
        board=registry[
            "bulletin_board"
        ],
        public_key=registry["K"],
        p=TEST_PARAMS.p,
        q=TEST_PARAMS.q,
        g=TEST_PARAMS.g,
    )

    verification = (
        verify_public_registry(
            json.dumps(registry)
        )
    )

    assert not verification["V4"]
    assert not verification["overall"]


def test_independent_verifier_rejects_duplicate_ballot():
    """
    Il verificatore rifiuta due schede con gli stessi ciphertext.
    """

    registry = build_public_registry(
        REPORT,
        TEST_PARAMS,
    )

    entries = registry[
        "bulletin_board"
    ]["entries"]

    entries[1]["ballot"] = deepcopy(
        entries[0]["ballot"]
    )

    assert not verify_v4_board_rules(
        board=registry[
            "bulletin_board"
        ],
        public_key=registry["K"],
        p=TEST_PARAMS.p,
        q=TEST_PARAMS.q,
        g=TEST_PARAMS.g,
    )


def test_e3_rejects_tampered_guardian_proof():
    """
    E3 rileva una modifica a una prova Schnorr dei garanti.
    """

    registry = build_public_registry(
        REPORT,
        TEST_PARAMS,
    )

    registry["guardians"][0]["proofs"][0][
        "challenge"
    ] += 1

    verification = verify_public_registry(
        json.dumps(registry)
    )

    assert not verification["V2"]
    assert not verification["overall"]


def test_e3_rejects_tampered_ballot_proof():
    """
    E3 rileva una modifica a una prova della scheda.
    """

    registry = build_public_registry(
        REPORT,
        TEST_PARAMS,
    )

    registry["bulletin_board"]["entries"][0][
        "proofs"
    ]["r1_proofs"][0]["branches"][0][
        "challenge"
    ] += 1

    verification = verify_public_registry(
        json.dumps(registry)
    )

    assert not verification["V3"]
    assert not verification["overall"]


def test_e3_rejects_tampered_tracking_code():
    """
    E3 rileva una modifica alla catena della bacheca.
    """

    registry = build_public_registry(
        REPORT,
        TEST_PARAMS,
    )

    registry["bulletin_board"]["entries"][0][
        "tracking_code"
    ] += 1

    verification = verify_public_registry(
        json.dumps(registry)
    )

    assert not verification["V4"]
    assert not verification["overall"]


def test_e3_rejects_tampered_decryption_proof():
    """
    E3 rileva una modifica a una prova Chaum-Pedersen.
    """

    registry = build_public_registry(
        REPORT,
        TEST_PARAMS,
    )

    registry["district_results"][0][
        "decryption_shares"
    ][0][0]["proof"]["challenge"] += 1

    verification = verify_public_registry(
        json.dumps(registry)
    )

    assert not verification["V6"]
    assert not verification["overall"]


def test_e3_rejects_wrong_clear_total():
    """
    E3 rileva un totale in chiaro incompatibile con la decifratura.
    """

    registry = build_public_registry(
        REPORT,
        TEST_PARAMS,
    )

    result = registry[
        "district_results"
    ][0]

    original = result[
        "list_votes"
    ][0]

    if original < result["ballot_count"]:
        result["list_votes"][0] += 1
    else:
        result["list_votes"][0] -= 1

    registry["scrutiny"] = recompute_v8_scrutiny(
        configuration=registry[
            "configuration"
        ],
        district_results=registry[
            "district_results"
        ],
    )

    verification = verify_public_registry(
        json.dumps(registry)
    )

    assert not verification["V7"]
    assert verification["V8"]
    assert not verification["overall"]


def test_e3_rejects_tampered_scrutiny():
    """
    E3 rileva una modifica al risultato dello scrutinio.
    """

    registry = build_public_registry(
        REPORT,
        TEST_PARAMS,
    )

    registry["scrutiny"][
        "list_seats"
    ][0] += 1

    verification = verify_public_registry(
        json.dumps(registry)
    )

    assert not verification["V8"]
    assert not verification["overall"]


def _rewrite_configuration(registry: dict) -> None:
    """
    Riscrive il registro come farebbe chi vuole cambiare la legge
    elettorale dopo il voto: la soglia del premio sale al 50% e lo
    scrutinio viene rifatto con la nuova regola, così V8 torna.
    """

    registry["configuration"]["rules"]["bonus_threshold_percent"] = 50
    registry["scrutiny"] = recompute_v8_scrutiny(
        registry["configuration"],
        registry["district_results"],
    )


def test_e3_rejects_rewritten_configuration():
    """
    E3: configurazione e seggi riscritti in modo coerente. Lo scrutinio
    torna (V8), ma l'impronta della configurazione non coincide più con
    il valore e del contesto e V2 fallisce.
    """

    registry = build_public_registry(
        REPORT,
        TEST_PARAMS,
    )

    _rewrite_configuration(registry)

    verification = verify_public_registry(
        json.dumps(registry)
    )

    assert verification["V8"]
    assert not verification["V2"]
    assert not verification["overall"]


def test_e3_rejects_configuration_rewritten_with_its_context():
    """
    E3: anche ricalcolando e, Q e Q_bar dalla configurazione nuova,
    le prove dei garanti, delle schede e delle share erano legate al
    contesto vecchio e non tornano più.
    """

    registry = build_public_registry(
        REPORT,
        TEST_PARAMS,
    )

    _rewrite_configuration(registry)

    group = registry["group"]
    context = registry["election_context"]
    q = group["q"]

    context["e"] = configuration_fingerprint(
        registry["configuration"],
        q,
    )
    context["Q"] = hash_to_q(
        q,
        group["p"],
        q,
        group["g"],
        context["n"],
        context["quorum"],
        context["e"],
    )
    context["Q_bar"] = hash_to_q(
        q,
        context["Q"],
        registry["K"],
    )
    registry["bulletin_board"]["extended_base_hash"] = context["Q_bar"]

    verification = verify_public_registry(
        json.dumps(registry)
    )

    failed = {
        name
        for name, valid in verification.items()
        if name != "overall" and not valid
    }

    assert {"V2", "V3", "V4", "V6"} <= failed
    assert verification["V8"]
    assert not verification["overall"]


def test_complete_registry_with_demo_parameters():
    """
    L'intero protocollo funziona con i parametri finali della demo.
    """

    report = simulate_election(
        config=CONFIG,
        voters_per_district=2,
        guardian_count=3,
        quorum=2,
        present_guardians=(1, 3),
        params=DEMO_PARAMS,
        seed=7,
        list_weights=WEIGHTS,
        spoil_probability=0.2,
    )

    serialized = public_registry_to_json(
        report,
        DEMO_PARAMS,
    )

    verification = verify_public_registry(
        serialized
    )

    assert verification == {
        "V1": True,
        "V2": True,
        "V3": True,
        "V4": True,
        "V5": True,
        "V6": True,
        "V7": True,
        "V8": True,
        "overall": True,
    }