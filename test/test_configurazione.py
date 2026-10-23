"""
Test unitari per evoto.configurazione.

Usiamo sia il file config/elezione_esempio.json sia piccole
configurazioni costruite nel test per i casi di errore.
"""

import copy
from fractions import Fraction
from pathlib import Path

import pytest

from evoto.configurazione import (
    build_ballot_layout,
    canonical_config_json,
    coalition_of,
    config_fingerprint,
    election_config_to_data,
    load_election_config,
    parse_election_config,
    preference_candidates,
)
from evoto.gruppo import DEMO_PARAMS, TEST_PARAMS
from evoto.scheda import PreferenceMetadata


EXAMPLE_PATH = (
    Path(__file__).resolve().parent.parent
    / "config"
    / "elezione_esempio.json"
)


def small_config_data() -> dict:
    """
    Configurazione minima: due liste in coalizione, una singola,
    una circoscrizione.

    La Lista B ha solo il capolista, quindi i seggi sono uno solo:
    ogni lista deve avere almeno tanti candidati quanti sono i seggi.
    """

    return {
        "name": "Test",
        "election_id": 5,
        "seats": 1,
        "rules": {
            "max_preferences": 3,
            "list_threshold_percent": 3,
            "coalition_threshold_percent": 10,
            "bonus_threshold_percent": 42,
            "bonus_seats_percent": 55,
        },
        "lists": [
            {"name": "Lista A", "coalition": "Alfa"},
            {"name": "Lista B", "coalition": "Alfa"},
            {"name": "Lista C", "coalition": None},
        ],
        "districts": [
            {
                "name": "Unica",
                "candidates": {
                    "Lista A": [
                        {"name": "A0", "gender": "M"},
                        {"name": "A1", "gender": "F"},
                        {"name": "A2", "gender": "M"},
                    ],
                    "Lista B": [
                        {"name": "B0", "gender": "F"},
                    ],
                    "Lista C": [
                        {"name": "C0", "gender": "F"},
                        {"name": "C1", "gender": "M"},
                    ],
                },
            },
        ],
    }


def test_example_config_is_valid():
    """
    Il file di esempio rispetta la struttura richiesta dall'esperimento E1:
    tre circoscrizioni, sei liste in due coalizioni e due liste singole.
    """

    config = load_election_config(EXAMPLE_PATH)

    assert config.election_id == 1
    assert config.seats == 15
    assert len(config.list_names) == 8
    assert len(config.districts) == 3

    coalitions = {
        coalition.name: coalition.list_indices
        for coalition in config.coalitions
    }

    assert coalitions == {
        "Coalizione Alfa": (0, 1, 3),
        "Coalizione Beta": (4, 5, 6),
    }


def test_example_config_rules_are_exact_fractions():
    """
    Le soglie sono frazioni esatte, non numeri in virgola mobile.
    """

    rules = load_election_config(EXAMPLE_PATH).rules

    assert rules.list_threshold == Fraction(3, 100)
    assert rules.coalition_threshold == Fraction(10, 100)
    assert rules.bonus_threshold == Fraction(42, 100)
    assert rules.bonus_seat_share == Fraction(55, 100)


def test_example_layout_of_the_first_district():
    """
    Nella circoscrizione Nord ogni lista ha capolista e quattro candidati:
    8 liste per 4 caselle danno 32 caselle di preferenza.

    Le prime quattro sono quelle della Lista A dell'esempio della
    proposta di progetto: Galli M., Conti L., Marino P., Greco S.
    """

    config = load_election_config(EXAMPLE_PATH)

    layout = build_ballot_layout(config, 0)

    assert layout.list_count == 8
    assert len(layout.preference_metadata) == 32

    assert layout.preference_metadata[:4] == (
        PreferenceMetadata(list_index=0, gender="M"),
        PreferenceMetadata(list_index=0, gender="F"),
        PreferenceMetadata(list_index=0, gender="M"),
        PreferenceMetadata(list_index=0, gender="F"),
    )


def test_parse_small_config():
    """
    Le coalizioni sono ricavate dal campo coalition delle liste.
    """

    config = parse_election_config(small_config_data())

    assert config.list_names == ("Lista A", "Lista B", "Lista C")
    assert len(config.coalitions) == 1
    assert config.coalitions[0].name == "Alfa"
    assert config.coalitions[0].list_indices == (0, 1)


def test_coalition_of():
    """
    Una lista non coalizzata non ha coalizione.
    """

    config = parse_election_config(small_config_data())

    assert coalition_of(config, 0) == 0
    assert coalition_of(config, 1) == 0
    assert coalition_of(config, 2) is None


def test_preference_candidates_skip_the_head_of_list():
    """
    Il capolista (posizione 0) non ha casella di preferenza.

    La Lista B ha solo il capolista, quindi nessuna casella.
    """

    config = parse_election_config(small_config_data())

    assert preference_candidates(config, 0) == (
        (0, 1),
        (0, 2),
        (2, 1),
    )


def test_layout_follows_preference_candidates():
    """
    Le caselle del layout seguono lo stesso ordine dei candidati.
    """

    config = parse_election_config(small_config_data())

    layout = build_ballot_layout(config, 0)

    assert layout.list_count == 3
    assert layout.preference_metadata == (
        PreferenceMetadata(list_index=0, gender="F"),
        PreferenceMetadata(list_index=0, gender="M"),
        PreferenceMetadata(list_index=2, gender="M"),
    )


def test_decimal_percentages_are_exact():
    """
    Una soglia del 2,5% diventa esattamente 1/40.
    """

    data = small_config_data()
    data["rules"]["list_threshold_percent"] = 2.5

    config = parse_election_config(data)

    assert config.rules.list_threshold == Fraction(1, 40)


def test_rejects_duplicated_list_names():
    """
    Due liste non possono avere lo stesso nome.
    """

    data = small_config_data()
    data["lists"][1]["name"] = "Lista A"

    with pytest.raises(ValueError):
        parse_election_config(data)


def test_rejects_district_without_a_list():
    """
    Ogni circoscrizione deve avere candidati per tutte le liste.
    """

    data = small_config_data()
    del data["districts"][0]["candidates"]["Lista C"]

    with pytest.raises(ValueError):
        parse_election_config(data)


def test_rejects_list_without_head_of_list():
    """
    Ogni lista deve avere almeno il capolista.
    """

    data = small_config_data()
    data["districts"][0]["candidates"]["Lista B"] = []

    with pytest.raises(ValueError):
        parse_election_config(data)


def test_rejects_missing_field():
    """
    Un campo mancante produce un ValueError, non un KeyError.
    """

    data = small_config_data()
    del data["rules"]["bonus_threshold_percent"]

    with pytest.raises(ValueError):
        parse_election_config(data)


def test_rejects_percentage_above_one_hundred():
    """
    Una percentuale deve stare tra 0 e 100.
    """

    data = small_config_data()
    data["rules"]["coalition_threshold_percent"] = 120

    with pytest.raises(ValueError):
        parse_election_config(data)


def test_rejects_non_positive_seats():
    """
    Il numero di seggi deve essere positivo.
    """

    data = small_config_data()
    data["seats"] = 0

    with pytest.raises(ValueError):
        parse_election_config(data)


def test_layout_uses_configured_preference_limit():
    """
    Il layout usa il limite di preferenze definito nella configurazione.
    """

    data = copy.deepcopy(small_config_data())
    data["rules"]["max_preferences"] = 2

    config = parse_election_config(data)
    layout = build_ballot_layout(config, 0)

    assert layout.max_preferences == 2


def test_layout_rejects_unknown_district():
    """
    L'indice della circoscrizione deve esistere.
    """

    config = parse_election_config(small_config_data())

    with pytest.raises(ValueError):
        build_ballot_layout(config, 1)


def test_rejects_lists_with_fewer_candidates_than_seats():
    """
    Con 2 seggi la Lista B, che ha solo il capolista, non potrebbe
    coprire tutti i seggi vinti: la configurazione è rifiutata.

    Nell'esempio ogni lista ha 5 candidati in 3 circoscrizioni,
    cioè 15, quanti i seggi.
    """

    data = small_config_data()
    data["seats"] = 2

    with pytest.raises(ValueError, match="Lista B"):
        parse_election_config(data)

    config = load_election_config(EXAMPLE_PATH)

    for list_index in range(len(config.list_names)):
        total = sum(
            len(district.candidates[list_index])
            for district in config.districts
        )

        assert total >= config.seats


def test_genders_are_normalized():
    """
    "m" e " M " sono lo stesso genere per la regola R5.
    """

    data = small_config_data()
    data["districts"][0]["candidates"]["Lista A"][2]["gender"] = " m "

    config = parse_election_config(data)

    assert config.districts[0].candidates[0][2].gender == "M"


def test_rejects_the_old_gender_limit():
    """
    Il vecchio limite per genere non esiste più: la regola di genere
    segue la legge. Un file che lo contiene viene rifiutato, così non si
    pensa che quel limite venga applicato.
    """

    data = small_config_data()
    data["rules"]["max_preferences_per_gender"] = 2

    with pytest.raises(ValueError, match="max_preferences_per_gender"):
        parse_election_config(data)


def test_rejects_more_than_three_preferences():
    """
    La regola di genere coincide con quella della legge fino a tre
    preferenze, quindi la configurazione non ne ammette di più.
    """

    data = small_config_data()
    data["rules"]["max_preferences"] = 4

    with pytest.raises(ValueError, match="max_preferences"):
        parse_election_config(data)


def test_rejects_more_than_two_genders():
    """
    La regola di genere confronta due generi.
    """

    data = small_config_data()
    data["districts"][0]["candidates"]["Lista C"][1]["gender"] = "X"

    with pytest.raises(ValueError, match="due generi"):
        parse_election_config(data)


def test_fingerprint_does_not_depend_on_key_order_or_spacing():
    """
    L'impronta si calcola sul JSON canonico: l'ordine delle chiavi nel
    file e il modo di scrivere il genere non contano.
    """

    data = small_config_data()

    reordered = dict(reversed(list(copy.deepcopy(data).items())))
    reordered["districts"][0]["candidates"]["Lista A"][1]["gender"] = " f "

    first = parse_election_config(data)
    second = parse_election_config(reordered)

    assert config_fingerprint(first, TEST_PARAMS) == config_fingerprint(
        second,
        TEST_PARAMS,
    )


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("rules", "bonus_threshold_percent", 50),
        ("rules", "list_threshold_percent", 4),
        ("rules", "max_preferences", 2),
        (None, "seats", 2),
        (None, "election_id", 6),
    ],
)
def test_fingerprint_changes_with_the_rules(section, field, value):
    """
    Cambiare una qualunque regola cambia l'impronta, quindi il
    contesto Q e tutte le prove dell'elezione.
    """

    data = small_config_data()
    data["districts"][0]["candidates"]["Lista B"].append(
        {"name": "B1", "gender": "M"}
    )
    data["districts"][0]["candidates"]["Lista C"].append(
        {"name": "C2", "gender": "F"}
    )

    original = config_fingerprint(parse_election_config(data), DEMO_PARAMS)

    changed = copy.deepcopy(data)
    target = changed if section is None else changed[section]
    target[field] = value

    assert config_fingerprint(
        parse_election_config(changed),
        DEMO_PARAMS,
    ) != original


def test_fingerprint_is_sha256_of_the_canonical_json():
    """
    L'impronta è SHA-256 del JSON canonico ridotto modulo q: la si può
    ricalcolare senza la libreria, come fa il verificatore.
    """

    import hashlib
    import json

    config = load_election_config(EXAMPLE_PATH)
    data = election_config_to_data(config)
    canonical = canonical_config_json(data)

    assert canonical == json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert canonical.startswith('{"districts":[')

    expected = int.from_bytes(
        hashlib.sha256(canonical.encode("utf-8")).digest(),
        "big",
    ) % DEMO_PARAMS.q

    assert config_fingerprint(config, DEMO_PARAMS) == expected
