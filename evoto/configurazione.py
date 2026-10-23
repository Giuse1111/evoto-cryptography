"""
Configurazione dell'elezione del progetto evoto.

Questo modulo contiene:
- il modello della configurazione: liste, coalizioni, circoscrizioni,
  candidati e regole elettorali;
- la lettura e la validazione del file JSON di configurazione;
- la costruzione del BallotLayout di ogni circoscrizione;
- la corrispondenza tra caselle di preferenza e candidati.

Cambiare legge elettorale significa cambiare il file di configurazione,
non il codice. Il formato del file è descritto nella specifica condivisa
docs/spec_f1.md, sezione 42.
"""

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
from pathlib import Path

from evoto.gruppo import GroupParameters
from evoto.scheda import (
    MAX_GENDERS,
    MAX_PREFERENCES_WITH_GENDER_RULE,
    BallotLayout,
    PreferenceMetadata,
)


@dataclass(frozen=True)
class Candidate:
    """
    Candidato di una lista in una circoscrizione.

    gender è il genere usato dalla regola di genere R5.
    """

    name: str
    gender: str


@dataclass(frozen=True)
class Coalition:
    """
    Coalizione di liste collegate.

    list_indices contiene gli indici delle liste che ne fanno parte.
    """

    name: str
    list_indices: tuple[int, ...]


@dataclass(frozen=True)
class District:
    """
    Circoscrizione elettorale.

    candidates[p] contiene i candidati della lista p in questa
    circoscrizione: il primo è il capolista bloccato, che non ha una
    casella di preferenza; gli altri hanno una casella ciascuno.
    """

    name: str
    candidates: tuple[tuple[Candidate, ...], ...]


@dataclass(frozen=True)
class ElectoralRules:
    """
    Parametri della legge elettorale.

    Le soglie sono frazioni dei voti validi nazionali:
    per esempio Fraction(3, 100) significa 3%.

    La regola di genere non ha parametri: segue la legge (sezione 17
    della specifica), quindi non compare qui.
    """

    max_preferences: int
    list_threshold: Fraction
    coalition_threshold: Fraction
    bonus_threshold: Fraction
    bonus_seat_share: Fraction


@dataclass(frozen=True)
class ElectionConfig:
    """
    Configurazione completa di un'elezione.

    election_id è un numero che identifica l'elezione per le persone
    (per esempio genera le carte d'identità fittizie della cabina).
    Nel contesto Q della cerimonia non entra election_id ma l'impronta
    dell'intera configurazione, calcolata da config_fingerprint.
    """

    name: str
    election_id: int
    seats: int
    rules: ElectoralRules
    list_names: tuple[str, ...]
    coalitions: tuple[Coalition, ...]
    districts: tuple[District, ...]


def _percent(value: object, field: str) -> Fraction:
    """
    Converte una percentuale del file JSON in una frazione esatta.

    Passiamo da str() per evitare gli errori di arrotondamento
    dei numeri in virgola mobile: 2.5 diventa esattamente 1/40.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Il campo {field} deve essere un numero.")

    fraction = Fraction(str(value)) / 100

    if not 0 <= fraction <= 1:
        raise ValueError(f"Il campo {field} deve essere compreso tra 0 e 100.")

    return fraction


def _positive_int(value: object, field: str) -> int:
    """
    Controlla che un campo del file JSON sia un intero positivo.
    """

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"Il campo {field} deve essere un intero positivo.")

    return value


def _parse_rules(data: dict) -> ElectoralRules:
    """
    Legge la sezione rules del file di configurazione.

    Le preferenze sono al massimo tre, come nella legge: la regola di
    genere che dimostriamo (R5) coincide con quella della legge solo
    fino a tre preferenze (sezione 17 della specifica).
    """

    if "max_preferences_per_gender" in data:
        raise ValueError(
            "Il campo max_preferences_per_gender non esiste più: la regola "
            "di genere segue la legge e non ha parametri (sezione 17 della "
            "specifica)."
        )

    max_preferences = data["max_preferences"]

    if (
        isinstance(max_preferences, bool)
        or not isinstance(max_preferences, int)
        or max_preferences < 0
    ):
        raise ValueError(
            "Il campo max_preferences deve essere un intero non negativo."
        )

    if max_preferences > MAX_PREFERENCES_WITH_GENDER_RULE:
        raise ValueError(
            "Il campo max_preferences non può superare "
            f"{MAX_PREFERENCES_WITH_GENDER_RULE}: la regola di genere è "
            "definita per al massimo tre preferenze."
        )

    bonus_seat_share = _percent(
        data["bonus_seats_percent"],
        "bonus_seats_percent",
    )

    if bonus_seat_share == 0:
        raise ValueError("Il campo bonus_seats_percent deve essere positivo.")

    return ElectoralRules(
        max_preferences=max_preferences,
        list_threshold=_percent(
            data["list_threshold_percent"],
            "list_threshold_percent",
        ),
        coalition_threshold=_percent(
            data["coalition_threshold_percent"],
            "coalition_threshold_percent",
        ),
        bonus_threshold=_percent(
            data["bonus_threshold_percent"],
            "bonus_threshold_percent",
        ),
        bonus_seat_share=bonus_seat_share,
    )


def _parse_candidate(data: dict) -> Candidate:
    """
    Legge un candidato e controlla nome e genere.

    Il genere viene normalizzato (spazi tolti, maiuscolo): "f" e "F "
    sono lo stesso genere per la regola R5.
    """

    name = data["name"]
    gender = data["gender"]

    if not isinstance(name, str) or not name.strip():
        raise ValueError("Ogni candidato deve avere un nome.")

    if not isinstance(gender, str) or not gender.strip():
        raise ValueError(f"Il candidato {name} deve avere un genere.")

    return Candidate(
        name=name,
        gender=gender.strip().upper(),
    )


def parse_election_config(data: dict) -> ElectionConfig:
    """
    Costruisce e valida la configurazione a partire dal JSON già letto.

    Controlliamo, tra l'altro, che:
    - i nomi delle liste siano distinti;
    - ogni circoscrizione abbia candidati per tutte e sole le liste;
    - ogni lista abbia almeno il capolista in ogni circoscrizione;
    - ogni lista abbia in tutto almeno tanti candidati quanti sono i
      seggi, così lo scrutinio ha sempre qualcuno da eleggere per ogni
      seggio vinto (sezione 42 della specifica);
    - i generi dei candidati siano al massimo due, come richiede la
      regola di genere.

    Le coalizioni sono ricavate dal campo coalition di ogni lista,
    nell'ordine in cui compaiono per la prima volta.

    Un campo mancante produce un ValueError che lo nomina.
    """

    try:
        return _parse_election_config(data)
    except KeyError as exc:
        raise ValueError(f"Manca il campo obbligatorio {exc}.") from exc


def _parse_election_config(data: dict) -> ElectionConfig:
    """
    Corpo di parse_election_config: può sollevare KeyError
    se manca un campo, che viene tradotto dal chiamante.
    """

    name = data["name"]
    election_id = data["election_id"]
    seats = _positive_int(data["seats"], "seats")
    rules = _parse_rules(data["rules"])
    lists_data = data["lists"]
    districts_data = data["districts"]

    if isinstance(election_id, bool) or not isinstance(election_id, int):
        raise ValueError("Il campo election_id deve essere un intero.")

    if election_id < 0:
        raise ValueError("Il campo election_id deve essere non negativo.")

    if not lists_data:
        raise ValueError("L'elezione deve avere almeno una lista.")

    list_names = tuple(entry["name"] for entry in lists_data)

    if len(set(list_names)) != len(list_names):
        raise ValueError("I nomi delle liste devono essere distinti.")

    # Le coalizioni sono ricavate dalle liste che le dichiarano.
    coalition_members: dict[str, list[int]] = {}

    for index, entry in enumerate(lists_data):
        coalition = entry.get("coalition")

        if coalition is None:
            continue

        if not isinstance(coalition, str) or not coalition.strip():
            raise ValueError("Il nome della coalizione non è valido.")

        coalition_members.setdefault(coalition, []).append(index)

    coalitions = tuple(
        Coalition(
            name=coalition,
            list_indices=tuple(indices),
        )
        for coalition, indices in coalition_members.items()
    )

    if not districts_data:
        raise ValueError("L'elezione deve avere almeno una circoscrizione.")

    districts = []

    for district_data in districts_data:
        district_name = district_data["name"]
        candidates_data = district_data["candidates"]

        if set(candidates_data) != set(list_names):
            raise ValueError(
                f"La circoscrizione {district_name} deve avere candidati "
                "per tutte e sole le liste dell'elezione."
            )

        candidates = []

        for list_name in list_names:
            list_candidates = tuple(
                _parse_candidate(candidate)
                for candidate in candidates_data[list_name]
            )

            if not list_candidates:
                raise ValueError(
                    f"La {list_name} nella circoscrizione {district_name} "
                    "deve avere almeno il capolista."
                )

            candidates.append(list_candidates)

        districts.append(
            District(
                name=district_name,
                candidates=tuple(candidates),
            )
        )

    genders = {
        candidate.gender
        for district in districts
        for list_candidates in district.candidates
        for candidate in list_candidates
    }

    if len(genders) > MAX_GENDERS:
        raise ValueError(
            "I candidati devono avere al massimo due generi: la regola di "
            f"genere confronta due generi, qui ce ne sono {len(genders)}."
        )

    # Una lista non può vincere più seggi dei candidati che presenta:
    # senza questo vincolo lo scrutinio si fermerebbe (sezione 46).
    for list_index, list_name in enumerate(list_names):
        total = sum(
            len(district.candidates[list_index])
            for district in districts
        )

        if total < seats:
            raise ValueError(
                f"La {list_name} ha {total} candidati in tutto, meno dei "
                f"{seats} seggi: ogni lista deve avere almeno tanti "
                "candidati quanti sono i seggi."
            )

    return ElectionConfig(
        name=name,
        election_id=election_id,
        seats=seats,
        rules=rules,
        list_names=list_names,
        coalitions=coalitions,
        districts=tuple(districts),
    )


def load_election_config(path: str | Path) -> ElectionConfig:
    """
    Legge e valida un file JSON di configurazione.
    """

    with open(path, encoding="utf-8") as config_file:
        data = json.load(config_file)

    return parse_election_config(data)


def fraction_to_percent(
    value: Fraction,
) -> int | float:
    """
    Riporta una frazione alla percentuale del file di configurazione.
    """

    percent = value * 100

    if percent.denominator == 1:
        return percent.numerator

    return float(percent)


def election_config_to_data(
    config: ElectionConfig,
) -> dict[str, object]:
    """
    Ricostruisce la configurazione nel formato JSON ufficiale.

    È la configurazione che compare nel registro pubblico, già
    normalizzata (per esempio i generi in maiuscolo), ed è anche quella
    di cui si calcola l'impronta.
    """

    coalition_by_list: dict[int, str] = {}

    for coalition in config.coalitions:
        for list_index in coalition.list_indices:
            coalition_by_list[list_index] = coalition.name

    lists = [
        {
            "name": list_name,
            "coalition": coalition_by_list.get(list_index),
        }
        for list_index, list_name in enumerate(config.list_names)
    ]

    districts = []

    for district in config.districts:
        candidates = {
            config.list_names[list_index]: [
                {
                    "name": candidate.name,
                    "gender": candidate.gender,
                }
                for candidate in list_candidates
            ]
            for list_index, list_candidates in enumerate(
                district.candidates
            )
        }

        districts.append(
            {
                "name": district.name,
                "candidates": candidates,
            }
        )

    return {
        "name": config.name,
        "election_id": config.election_id,
        "seats": config.seats,
        "rules": {
            "max_preferences": config.rules.max_preferences,
            "list_threshold_percent": fraction_to_percent(
                config.rules.list_threshold
            ),
            "coalition_threshold_percent": fraction_to_percent(
                config.rules.coalition_threshold
            ),
            "bonus_threshold_percent": fraction_to_percent(
                config.rules.bonus_threshold
            ),
            "bonus_seats_percent": fraction_to_percent(
                config.rules.bonus_seat_share
            ),
        },
        "lists": lists,
        "districts": districts,
    }


def canonical_config_json(
    data: dict[str, object],
) -> str:
    """
    Scrive la configurazione in forma canonica.

    Chiavi in ordine alfabetico, nessuno spazio, caratteri non ASCII
    lasciati come sono: due configurazioni uguali danno sempre la
    stessa stringa, qualunque sia l'ordine delle chiavi nel file.
    """

    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def config_fingerprint(
    config: ElectionConfig,
    params: GroupParameters,
) -> int:
    """
    Impronta della configurazione: SHA-256 del suo JSON canonico,
    ridotto modulo q.

    È il valore e del contesto Q = H(p, q, g, n, k, e) della cerimonia
    delle chiavi (sezione 35 della specifica), come l'hash del manifest
    in ElectionGuard. Così liste, candidati, soglie, premio e limiti
    delle preferenze sono legati a tutte le prove dell'elezione:
    cambiare la configurazione dopo la cerimonia cambia Q, e le prove
    dei garanti e delle schede non tornano più.
    """

    canonical = canonical_config_json(
        election_config_to_data(config)
    )

    digest = hashlib.sha256(
        canonical.encode("utf-8")
    ).digest()

    return int.from_bytes(digest, "big") % params.q


def coalition_of(
    config: ElectionConfig,
    list_index: int,
) -> int | None:
    """
    Restituisce l'indice della coalizione della lista, oppure None
    se la lista non è coalizzata.
    """

    for coalition_index, coalition in enumerate(config.coalitions):
        if list_index in coalition.list_indices:
            return coalition_index

    return None


def preference_candidates(
    config: ElectionConfig,
    district_index: int,
) -> tuple[tuple[int, int], ...]:
    """
    Associa ogni casella di preferenza al suo candidato.

    L'elemento k è la coppia (indice della lista, posizione del
    candidato nella lista) della k-esima casella di preferenza.
    Le posizioni partono da 1 perché la posizione 0 è il capolista,
    che non ha casella.

    L'ordine delle caselle è: lista dopo lista, nell'ordine della
    configurazione, e dentro ogni lista nell'ordine dei candidati.
    """

    district = config.districts[district_index]

    return tuple(
        (list_index, position)
        for list_index, candidates in enumerate(district.candidates)
        for position in range(1, len(candidates))
    )


def build_ballot_layout(
    config: ElectionConfig,
    district_index: int,
) -> BallotLayout:
    """
    Costruisce la struttura pubblica della scheda di una circoscrizione.

    I metadati delle preferenze (lista e genere) vengono dalla
    configurazione ufficiale e non dal votante.
    """

    if not 0 <= district_index < len(config.districts):
        raise ValueError("L'indice della circoscrizione non è valido.")

    district = config.districts[district_index]

    metadata = tuple(
        PreferenceMetadata(
            list_index=list_index,
            gender=district.candidates[list_index][position].gender,
        )
        for list_index, position in preference_candidates(
            config,
            district_index,
        )
    )

    return BallotLayout(
        list_count=len(config.list_names),
        preference_metadata=metadata,
        max_preferences=config.rules.max_preferences,
    )
