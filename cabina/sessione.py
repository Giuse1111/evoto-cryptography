"""
Stato dell'elezione dimostrativa della cabina web.

Questo modulo non dipende da Flask: contiene tutta la logica della
demo, che l'applicazione web si limita a mostrare.

- all'avvio: cerimonia delle chiavi con i garanti simulati ed elettori
  simulati che votano a caso, così la bacheca non parte vuota;
- durante il voto: carte d'identità fittizie per votare dal browser,
  scheda cifrata con le prove R1-R5, deposito oppure sfida di Benaloh;
- alla chiusura: conteggio omomorfico e decifratura a soglia con i
  garanti presenti scelti nella pagina, poi lo scrutinio;
- alla fine: registro pubblico JSON, verificatore indipendente e
  manomissioni dimostrative del registro.

La cabina è solo una vetrina: la validità delle schede non dipende
dall'interfaccia ma dalle prove, che la bacheca controlla sempre.
La specifica è in docs/spec_f1.md, sezione 52.
"""

from collections.abc import Callable
from dataclasses import dataclass
import json
import random
import secrets
import threading
import time

from evoto.configurazione import (
    ElectionConfig,
    build_ballot_layout,
    coalition_of,
    config_fingerprint,
    preference_candidates,
)
from evoto.garanti import (
    KeyCeremony,
    run_key_ceremony,
)
from evoto.gruppo import GroupParameters
from evoto.registro import public_registry_to_json
from evoto.scheda import (
    BallotLayout,
    BallotWitness,
)
from evoto.scrutinio import (
    ScrutinyResult,
    run_scrutiny,
)
from evoto.simulazione import random_choice
from evoto.urna import (
    CAST,
    BoardEntry,
    BulletinBoard,
    DistrictResult,
    DistrictTally,
    VoterRoll,
    cast_voter_ballot,
    create_bulletin_board,
    create_voter_roll,
    decrypt_district_tally,
    spoil_ballot,
    tally_district,
    verify_board_chain,
    verify_spoiled_ballot,
)
from evoto.voto import (
    PreparedBallot,
    VoterChoice,
    apply_gender_rule,
    choice_to_plaintexts,
    prepare_ballot,
)
from verifica.verifica import (
    recompute_v8_scrutiny,
    verify_public_registry,
)


# Pesi delle liste per la configurazione di esempio, come in demo.py:
# la Coalizione Alfa ha circa il 46% dei voti e prende il premio.
EXAMPLE_WEIGHTS = (22, 14, 12, 9, 16, 11, 7, 6)

# Carte d'identità fittizie per votare dal browser in ogni circoscrizione.
DEMO_CODES_PER_DISTRICT = 10

# Lettere dei numeri di carta d'identità fittizi: senza I, O e Q, che si
# confondono con 1 e 0.
CARD_LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"

# Probabilità che un elettore simulato sprechi una scheda per
# controllare il dispositivo, come nella simulazione di E1.
SIMULATED_SPOIL_PROBABILITY = 0.05

# Descrizione dei controlli del verificatore indipendente.
CHECK_DESCRIPTIONS = {
    "V1": "Parametri del gruppo e chiave pubblica",
    "V2": "Impronta della configurazione, Q, prove di Schnorr dei garanti, chiave congiunta e Q_bar",
    "V3": "Prove R1-R5 di ogni scheda",
    "V4": "Catena dei codici, schede sprecate ricifrate, nessuna copia",
    "V5": "Totali cifrati ricalcolati dalle sole schede depositate",
    "V6": "Prove Chaum-Pedersen delle share di decifratura e quorum",
    "V7": "Totali in chiaro compatibili con i totali cifrati: B / M = g^t",
    "V8": "Scrutinio rifatto da zero: soglie, premio, seggi ed eletti",
}

# Gli stessi controlli spiegati a chi non conosce la crittografia.
PLAIN_CHECK_DESCRIPTIONS = {
    "V1": "I numeri di base dell'elezione sono ben formati",
    "V2": "Le regole pubblicate sono quelle dell'elezione e i garanti hanno creato la chiave correttamente",
    "V3": "Ogni scheda rispetta le regole",
    "V4": "La bacheca è integra: nessuna scheda tolta, cambiata o copiata",
    "V5": "I totali contengono tutte e sole le schede depositate",
    "V6": "I garanti hanno aperto i totali correttamente",
    "V7": "I voti pubblicati sono proprio quelli dei totali aperti",
    "V8": "Seggi ed eletti seguono le regole elettorali",
}

VOTING = "voto"
CLOSED = "chiusa"


@dataclass(frozen=True)
class PublicElectionRecord:
    """
    Dati pubblici dell'elezione, nella forma letta dal registro.

    build_public_registry legge soltanto questi campi del resoconto
    di una simulazione: la cabina li fornisce con la stessa forma.
    """

    config: ElectionConfig
    ceremony: KeyCeremony
    board: BulletinBoard
    tallies: tuple[DistrictTally, ...]
    results: tuple[DistrictResult, ...]
    scrutiny: ScrutinyResult


@dataclass(frozen=True)
class PendingBallot:
    """
    Scheda cifrata in attesa della decisione dell'elettore:
    deposito oppure sfida di Benaloh.

    token identifica la scheda nella pagina di conferma.
    """

    token: str
    voter_id: str
    choice: VoterChoice
    prepared: PreparedBallot


@dataclass(frozen=True)
class CandidateView:
    """
    Candidato con casella di preferenza, come appare sulla scheda.
    """

    box: int
    name: str
    gender: str


@dataclass(frozen=True)
class ListView:
    """
    Lista come appare sulla scheda: simbolo, capolista bloccato e
    candidati con casella di preferenza.
    """

    index: int
    name: str
    symbol: str
    head: str
    candidates: tuple[CandidateView, ...]


@dataclass(frozen=True)
class GroupView:
    """
    Gruppo di liste sulla scheda: una coalizione oppure le liste
    non coalizzate (title None).
    """

    title: str | None
    lists: tuple[ListView, ...]


@dataclass(frozen=True)
class SpoilReport:
    """
    Esito della sfida di Benaloh mostrato all'elettore.

    entry: riga SPOILED pubblicata sulla bacheca.
    voter_id: elettore che ha sfidato il dispositivo e può rivotare.
    decoded: la scelta che il dispositivo aveva cifrato, ricavata dai
    voti rivelati.
    matches_choice: i voti rivelati corrispondono alla scelta fatta.
    reencrypts: ricifrando voti e nonce rivelati si ottiene proprio
    la scheda pubblicata.
    """

    entry: BoardEntry
    voter_id: str
    decoded: str
    matches_choice: bool
    reencrypts: bool


@dataclass(frozen=True)
class VerificationReport:
    """
    Esito del verificatore indipendente su un registro.

    tampering è la manomissione applicata, oppure None.
    seconds è il tempo impiegato dal verificatore.
    """

    tampering: str | None
    checks: dict[str, bool]
    overall: bool
    seconds: float


def symbol_of(list_name: str) -> str:
    """
    Simbolo breve della lista: l'ultima parola del nome, al massimo
    due caratteri (Lista A -> A).
    """

    return list_name.split()[-1][:2].upper()


def format_code(value: int, params: GroupParameters, short: bool = False) -> str:
    """
    Codice di tracciamento o impronta in esadecimale maiuscolo, a
    gruppi di quattro cifre.

    La lunghezza è quella di q, così tutti i codici hanno la stessa
    forma. La versione breve mostra i primi otto caratteri.
    """

    width = (params.q.bit_length() + 3) // 4
    digits = format(value, "X").rjust(width, "0")

    if short:
        digits = digits[:8]

    return " ".join(
        digits[start:start + 4]
        for start in range(0, len(digits), 4)
    )


def normalize_code(text: str) -> str:
    """
    Tiene solo le cifre esadecimali di un codice scritto o copiato
    dall'elettore: spazi, trattini e puntini di sospensione vengono
    ignorati.
    """

    return "".join(
        character
        for character in text.upper()
        if character in "0123456789ABCDEF"
    )


def demo_codes(config: ElectionConfig, per_district: int) -> dict[str, int]:
    """
    Numeri delle carte d'identità fittizie degli elettori che votano dal
    browser, con la loro circoscrizione.

    I numeri hanno il formato della carta d'identità elettronica: due
    lettere, cinque cifre, due lettere (per esempio CA12345AB). Sono
    tutti diversi e sempre gli stessi per la stessa elezione, perché
    dipendono solo da election_id; sono inventati e non corrispondono a
    documenti reali. In un sistema reale l'elettore accederebbe con SPID
    o con la CIE e il suo PIN; qui la lista degli aventi diritto è
    simulata.
    """

    rng = random.Random(f"carte-{config.election_id}")
    codes: dict[str, int] = {}

    def letters() -> str:
        return "".join(rng.choice(CARD_LETTERS) for _ in range(2))

    for district_index in range(len(config.districts)):
        added = 0

        while added < per_district:
            code = f"{letters()}{rng.randrange(100000):05d}{letters()}"

            if code not in codes:
                codes[code] = district_index
                added += 1

    return codes


def normalize_card(text: str) -> str:
    """
    Numero di carta d'identità scritto o copiato dall'elettore: lettere
    maiuscole e cifre, senza spazi né trattini.
    """

    return "".join(
        character
        for character in text.upper()
        if character.isascii() and character.isalnum()
    )


def explain_violation(
    layout: BallotLayout,
    choice: VoterChoice,
) -> str | None:
    """
    Spiega quale regola della scheda viola una scelta, se ne viola una,
    per esempio "R4 (più di 3 preferenze)".

    Serve solo per il messaggio all'elettore: il rifiuto vero viene
    dalle prove, che non si riescono a costruire.

    R5 non compare: il dispositivo annulla prima di cifrare le
    preferenze che la regola di genere non ammette (apply_gender_rule),
    quindi una scelta non può violarla.
    """

    list_index = choice.list_index

    if list_index is None and choice.preferences:
        list_index = layout.preference_metadata[choice.preferences[0]].list_index

    for box in choice.preferences:
        if layout.preference_metadata[box].list_index != list_index:
            return "R3 (una preferenza è per un candidato di un'altra lista)"

    kept, _ = apply_gender_rule(layout, choice.preferences)

    if len(kept) > layout.max_preferences:
        return f"R4 (più di {layout.max_preferences} preferenze)"

    return None


def annulled_preferences(
    layout: BallotLayout,
    choice: VoterChoice,
) -> tuple[int, ...]:
    """
    Caselle di preferenza che la regola di genere annulla, per avvisare
    l'elettore nella pagina di conferma.
    """

    _, annulled = apply_gender_rule(layout, choice.preferences)

    return annulled


def _tamper_vote(data: dict) -> None:
    data["district_results"][0]["list_votes"][0] += 1


def _tamper_tracking_code(data: dict) -> None:
    q = data["group"]["q"]
    entry = data["bulletin_board"]["entries"][0]
    entry["tracking_code"] = (entry["tracking_code"] + 1) % q


def _tamper_ballot(data: dict) -> None:
    p = data["group"]["p"]
    g = data["group"]["g"]

    entry = next(
        entry
        for entry in data["bulletin_board"]["entries"]
        if entry["state"] == "CAST"
    )

    ciphertext = entry["ballot"]["list_ciphertexts"][0]
    ciphertext["beta"] = ciphertext["beta"] * g % p


def _tamper_share(data: dict) -> None:
    p = data["group"]["p"]
    g = data["group"]["g"]

    share = data["district_results"][0]["decryption_shares"][0][0]
    share["partial_decryption"] = share["partial_decryption"] * g % p


def _tamper_seats(data: dict) -> None:
    seats = data["scrutiny"]["list_seats"]

    giver = max(range(len(seats)), key=lambda index: seats[index])
    taker = min(range(len(seats)), key=lambda index: seats[index])

    seats[giver] -= 1
    seats[taker] += 1


def _tamper_guardian(data: dict) -> None:
    q = data["group"]["q"]
    proof = data["guardians"][0]["proofs"][0]
    proof["response"] = (proof["response"] + 1) % q


def _tamper_configuration(data: dict) -> None:
    # La soglia del premio sale al 50% e lo scrutinio viene rifatto con
    # la nuova regola, così seggi e regole restano coerenti tra loro:
    # V8 non vede nulla, se ne accorge V2 con l'impronta della
    # configurazione.
    data["configuration"]["rules"]["bonus_threshold_percent"] = 50
    data["scrutiny"] = recompute_v8_scrutiny(
        data["configuration"],
        data["district_results"],
    )


# Manomissioni dimostrative del registro (esperimento E3 dal vivo).
TAMPERINGS: dict[str, tuple[str, Callable[[dict], None]]] = {
    "voto": (
        "Un voto in più alla prima lista della prima circoscrizione",
        _tamper_vote,
    ),
    "seggi": (
        "Un seggio spostato da una lista a un'altra",
        _tamper_seats,
    ),
    "scheda": (
        "Una casella cifrata cambiata in una scheda depositata",
        _tamper_ballot,
    ),
    "codice": (
        "Il codice di tracciamento della prima scheda cambiato",
        _tamper_tracking_code,
    ),
    "share": (
        "La share di decifratura di un garante alterata",
        _tamper_share,
    ),
    "garante": (
        "La prova di Schnorr di un garante alterata",
        _tamper_guardian,
    ),
    "configurazione": (
        "La soglia del premio portata al 50% e seggi ricalcolati di conseguenza",
        _tamper_configuration,
    ),
}


class ElectionSession:
    """
    Elezione dimostrativa servita dalla cabina web.

    Lo stato cambia nel tempo (bacheca, aventi diritto, fase), quindi
    è una classe e non un dataclass immutabile; le strutture di evoto
    restano immutabili e vengono sostituite a ogni passo. Un lock
    protegge lo stato, perché il server può servire più richieste
    insieme.

    I garanti sono simulati nello stesso programma, come in
    simulate_election: le loro share segrete restano in memoria e non
    vengono mai pubblicate.
    """

    def __init__(
        self,
        config: ElectionConfig,
        params: GroupParameters,
        guardian_count: int = 5,
        quorum: int = 3,
        demo_codes_per_district: int = DEMO_CODES_PER_DISTRICT,
    ) -> None:
        self.config = config
        self.params = params
        self.guardian_count = guardian_count
        self.quorum = quorum

        self._lock = threading.RLock()

        self.ceremony = run_key_ceremony(
            guardian_count=guardian_count,
            quorum=quorum,
            params=params,
            election_id=config_fingerprint(config, params),
        )

        self.layouts = tuple(
            build_ballot_layout(config, district_index)
            for district_index in range(len(config.districts))
        )

        self.board = create_bulletin_board(
            self.ceremony.extended_base_hash,
            params,
        )

        self.demo_voters = demo_codes(config, demo_codes_per_district)
        self.roll: VoterRoll = create_voter_roll(dict(self.demo_voters))

        self.phase = VOTING
        self.present_guardians: tuple[int, ...] = ()
        self.tallies: tuple[DistrictTally, ...] = ()
        self.results: tuple[DistrictResult, ...] = ()
        self.scrutiny: ScrutinyResult | None = None

        self._pending: dict[str, PendingBallot] = {}
        self._receipts: dict[str, int] = {}
        self._spoil_reports: dict[str, SpoilReport] = {}
        self._clean_verification: VerificationReport | None = None

        # Tempo medio di una scheda simulata (cifratura, prove, deposito),
        # misurato da prefill: serve a stimare la durata delle simulazioni.
        self.seconds_per_ballot: float | None = None

    @property
    def public_key(self) -> int:
        """
        Chiave pubblica congiunta K.
        """

        return self.ceremony.joint_public_key

    @property
    def context(self) -> int:
        """
        Contesto esteso Q_bar delle prove.
        """

        return self.ceremony.extended_base_hash

    def prefill(
        self,
        voters_per_district: int,
        seed: int,
        progress: Callable[[int, int], None] | None = None,
        list_weights: tuple[float, ...] | None = None,
        blank_probability: float = 0.03,
        spoil_probability: float = SIMULATED_SPOIL_PROBABILITY,
    ) -> None:
        """
        Fa votare elettori simulati con scelte casuali ma valide.

        Ogni elettore passa dallo stesso percorso del browser: scheda
        cifrata con le prove, controllo delle prove sulla bacheca,
        deposito. Con probabilità spoil_probability spreca prima una
        scheda per controllare il dispositivo.

        list_weights orienta il voto verso alcune liste (default: i
        pesi di demo.py per la configurazione di esempio, altrimenti
        tutte uguali). progress, se indicato, riceve il numero di
        schede depositate e il totale.
        """

        rng = random.Random(seed)

        if list_weights is not None:
            weights = list_weights
        elif len(self.config.list_names) == len(EXAMPLE_WEIGHTS):
            weights = EXAMPLE_WEIGHTS
        else:
            weights = tuple(1.0 for _ in self.config.list_names)

        if len(weights) != len(self.config.list_names):
            raise ValueError("Serve un peso per ogni lista.")

        voters = {
            f"simulato-{district_index + 1}-{number + 1:04d}": district_index
            for district_index in range(len(self.config.districts))
            for number in range(voters_per_district)
        }

        with self._lock:
            self._require_voting()

            if any(voter_id in self.roll.voter_districts for voter_id in voters):
                raise ValueError("Gli elettori simulati sono già stati aggiunti.")

            # Gli elettori simulati si aggiungono agli aventi diritto.
            self.roll = VoterRoll(
                voter_districts={
                    **self.roll.voter_districts,
                    **voters,
                },
                voted=self.roll.voted,
            )

        total = len(voters)
        done = 0
        ballots = 0
        start = time.perf_counter()

        for voter_id, district_index in voters.items():
            choice = random_choice(
                rng,
                self.layouts[district_index],
                weights,
                blank_probability,
                self.config.rules.max_preferences,
            )

            if rng.random() < spoil_probability:
                self.spoil(self.prepare(voter_id, choice).token)
                ballots += 1

            self.cast(self.prepare(voter_id, choice).token)

            done += 1
            ballots += 1

            if progress is not None:
                progress(done, total)

        # Tempo medio di una scheda, depositata o sprecata.
        if ballots:
            self.seconds_per_ballot = (time.perf_counter() - start) / ballots

    def _require_voting(self) -> None:
        if self.phase != VOTING:
            raise ValueError("L'elezione è chiusa: non si può più votare.")

    def default_present_guardians(self) -> tuple[int, ...]:
        """
        Garanti proposti nella pagina di spoglio: 1, 3 e 5 se esistono
        e bastano, altrimenti i primi quorum garanti.
        """

        default = (1, 3, 5)

        if max(default) <= self.guardian_count and len(default) >= self.quorum:
            return default

        return tuple(range(1, self.quorum + 1))

    @property
    def clean_verification(self) -> VerificationReport | None:
        """
        Esito del verificatore sul registro originale, se è già stato
        eseguito dopo la chiusura.
        """

        return self._clean_verification

    @property
    def min_search_length(self) -> int:
        """
        Cifre minime per cercare un codice sulla bacheca: quattro, o
        tutto il codice se è più corto (gruppo didattico).
        """

        return min(4, (self.params.q.bit_length() + 3) // 4)

    def available_demo_codes(self) -> dict[str, int]:
        """
        Carte d'identità fittizie con cui non si è ancora votato.
        """

        with self._lock:
            return {
                code: district
                for code, district in self.demo_voters.items()
                if code not in self.roll.voted
            }

    def voter_district(self, voter_id: str) -> int:
        """
        Circoscrizione di un elettore che può ancora votare.

        Solleva ValueError se la carta d'identità non è tra gli aventi
        diritto, se l'elettore ha già votato o se l'elezione è chiusa.
        """

        with self._lock:
            self._require_voting()

            if not voter_id:
                raise ValueError("Scrivi il numero della carta d'identità.")

            if voter_id not in self.roll.voter_districts:
                raise ValueError(
                    f"La carta d'identità {voter_id} non è tra quelle degli "
                    "aventi diritto."
                )

            if voter_id in self.roll.voted:
                raise ValueError(
                    f"Con la carta d'identità {voter_id} si è già votato."
                )

            return self.roll.voter_districts[voter_id]

    def ballot_view(self, district_index: int) -> tuple[GroupView, ...]:
        """
        Struttura della scheda di una circoscrizione per la pagina web:
        prima le coalizioni, poi le liste non coalizzate.
        """

        district = self.config.districts[district_index]
        boxes = preference_candidates(self.config, district_index)

        def list_view(list_index: int) -> ListView:
            candidates = district.candidates[list_index]

            return ListView(
                index=list_index,
                name=self.config.list_names[list_index],
                symbol=symbol_of(self.config.list_names[list_index]),
                head=candidates[0].name,
                candidates=tuple(
                    CandidateView(
                        box=box,
                        name=candidates[position].name,
                        gender=candidates[position].gender,
                    )
                    for box, (owner, position) in enumerate(boxes)
                    if owner == list_index
                ),
            )

        groups = [
            GroupView(
                title=coalition.name,
                lists=tuple(list_view(index) for index in coalition.list_indices),
            )
            for coalition in self.config.coalitions
        ]

        singles = tuple(
            list_view(index)
            for index in range(len(self.config.list_names))
            if coalition_of(self.config, index) is None
        )

        if singles:
            groups.append(GroupView(title=None, lists=singles))

        return tuple(groups)

    def describe_choice(
        self,
        district_index: int,
        list_plaintexts: tuple[int, ...],
        blank_plaintext: int,
        preference_plaintexts: tuple[int, ...],
    ) -> str:
        """
        Descrive a parole i bit di una scheda: lista o scheda bianca,
        e i candidati con la preferenza.
        """

        district = self.config.districts[district_index]
        boxes = preference_candidates(self.config, district_index)

        if blank_plaintext == 1:
            text = "scheda bianca"
        else:
            text = ", ".join(
                self.config.list_names[index]
                for index, bit in enumerate(list_plaintexts)
                if bit == 1
            ) or "nessuna lista"

        names = [
            district.candidates[boxes[box][0]][boxes[box][1]].name
            for box, bit in enumerate(preference_plaintexts)
            if bit == 1
        ]

        if names:
            text += "; preferenze: " + ", ".join(names)

        return text

    def prepare(self, voter_id: str, choice: VoterChoice) -> PendingBallot:
        """
        Cifra la scelta dell'elettore e costruisce le prove R1-R5.

        Se la scelta viola una regola della scheda le prove non si
        possono costruire: il ValueError spiega quale regola è violata.
        """

        district_index = self.voter_district(voter_id)
        layout = self.layouts[district_index]

        try:
            prepared = prepare_ballot(
                layout,
                district_index,
                choice,
                self.public_key,
                self.params,
                self.context,
            )
        except ValueError as error:
            reason = None

            if all(
                0 <= box < len(layout.preference_metadata)
                for box in choice.preferences
            ):
                reason = explain_violation(layout, choice)

            message = (
                "Il dispositivo non è riuscito a costruire le prove a "
                "conoscenza zero"
            )

            if reason is not None:
                message += f": la scelta viola la regola {reason}"

            raise ValueError(f"{message}. Dettaglio: {error}") from error

        pending = PendingBallot(
            token=secrets.token_urlsafe(16),
            voter_id=voter_id,
            choice=choice,
            prepared=prepared,
        )

        with self._lock:
            self._pending[pending.token] = pending

        return pending

    def pending(self, token: str) -> PendingBallot:
        """
        Scheda cifrata in attesa di deposito o sfida.
        """

        with self._lock:
            if token not in self._pending:
                raise ValueError("La scheda non esiste o è già stata usata.")

            return self._pending[token]

    def cast(self, token: str) -> BoardEntry:
        """
        Deposita la scheda: la bacheca controlla le prove, l'elenco
        degli aventi diritto segna l'elettore, e l'elettore riceve il
        codice di tracciamento.
        """

        with self._lock:
            if token in self._receipts:
                # Doppio clic o pagina ricaricata: la scheda è già
                # depositata, si restituisce la stessa ricevuta.
                return self.receipt(token)

            self._require_voting()
            pending = self.pending(token)
            prepared = pending.prepared

            self.board, self.roll = cast_voter_ballot(
                board=self.board,
                roll=self.roll,
                voter_id=pending.voter_id,
                district_index=prepared.district_index,
                layout=self.layouts[prepared.district_index],
                ballot=prepared.ballot,
                proofs=prepared.proofs,
                public_key=self.public_key,
                params=self.params,
            )

            del self._pending[token]
            self._clean_verification = None

            entry = self.board.entries[-1]
            self._receipts[token] = entry.sequence

            return entry

    def receipt(self, token: str) -> BoardEntry:
        """
        Riga della bacheca della scheda depositata con questo token.
        """

        with self._lock:
            if token not in self._receipts:
                raise ValueError("Ricevuta non trovata.")

            return self.board.entries[self._receipts[token] - 1]

    def spoil(self, token: str) -> SpoilReport:
        """
        Sfida di Benaloh: la scheda viene pubblicata come sprecata
        insieme ai voti e ai nonce, così chiunque può ricifrarla e
        controllare che il dispositivo abbia cifrato la scelta fatta.
        L'elettore può poi votare di nuovo.
        """

        with self._lock:
            if token in self._spoil_reports:
                return self._spoil_reports[token]

            self._require_voting()
            pending = self.pending(token)

            if pending.voter_id in self.roll.voted:
                raise ValueError(
                    f"Con la carta d'identità {pending.voter_id} si è già "
                    "votato."
                )

            prepared = pending.prepared
            witness: BallotWitness = prepared.witness

            self.board = spoil_ballot(
                board=self.board,
                district_index=prepared.district_index,
                layout=self.layouts[prepared.district_index],
                ballot=prepared.ballot,
                proofs=prepared.proofs,
                witness=witness,
                public_key=self.public_key,
                params=self.params,
            )

            del self._pending[token]
            self._clean_verification = None

            entry = self.board.entries[-1]

        expected = choice_to_plaintexts(
            self.layouts[prepared.district_index],
            pending.choice,
        )

        revealed = (
            witness.list_plaintexts,
            witness.blank_plaintext,
            witness.preference_plaintexts,
        )

        report = SpoilReport(
            entry=entry,
            voter_id=pending.voter_id,
            decoded=self.describe_choice(prepared.district_index, *revealed),
            matches_choice=revealed == expected,
            reencrypts=verify_spoiled_ballot(
                entry.ballot,
                entry.revealed_witness,
                self.public_key,
                self.params,
            ),
        )

        with self._lock:
            self._spoil_reports[token] = report

        return report

    def spoil_report(self, token: str) -> SpoilReport:
        """
        Esito della sfida del dispositivo fatta con questo token.
        """

        with self._lock:
            if token not in self._spoil_reports:
                raise ValueError("Sfida non trovata.")

            return self._spoil_reports[token]

    def find_entries(self, text: str) -> tuple[BoardEntry, ...]:
        """
        Cerca sulla bacheca le righe il cui codice di tracciamento
        inizia con le cifre indicate: almeno quattro, oppure tutto il
        codice se è più corto (gruppo didattico).
        """

        prefix = normalize_code(text)

        if len(prefix) < self.min_search_length:
            return ()

        with self._lock:
            return tuple(
                entry
                for entry in self.board.entries
                if normalize_code(
                    format_code(entry.tracking_code, self.params)
                ).startswith(prefix)
            )

    def board_is_valid(self) -> bool:
        """
        Controllo V4 della catena dei codici di tracciamento.
        """

        with self._lock:
            return verify_board_chain(self.board, self.params)

    def close(self, present_guardians: tuple[int, ...]) -> None:
        """
        Chiude l'elezione e decifra i totali con i garanti presenti.

        Per ogni circoscrizione: conteggio omomorfico delle schede
        depositate, share di decifratura dei garanti presenti con le
        loro prove, combinazione di Lagrange. Poi lo scrutinio.

        Con meno di quorum garanti la decifratura è impossibile e
        l'elezione resta aperta.
        """

        present = tuple(sorted(set(present_guardians)))

        if any(not 1 <= index <= self.guardian_count for index in present):
            raise ValueError("Un garante indicato non esiste.")

        if len(present) < self.quorum:
            raise ValueError(
                f"Servono almeno {self.quorum} garanti su "
                f"{self.guardian_count}: con {len(present)} la chiave "
                "segreta non si può usare e i totali restano cifrati."
            )

        with self._lock:
            self._require_voting()

            tallies = tuple(
                tally_district(
                    board=self.board,
                    district_index=district_index,
                    layout=layout,
                    params=self.params,
                )
                for district_index, layout in enumerate(self.layouts)
            )

            results = tuple(
                decrypt_district_tally(
                    tally=tally,
                    secret_shares=self.ceremony.secret_shares,
                    present_guardians=present,
                    records=self.ceremony.records,
                    quorum=self.quorum,
                    params=self.params,
                    extended_base_hash=self.context,
                )
                for tally in tallies
            )

            self.scrutiny = run_scrutiny(self.config, results)
            self.tallies = tallies
            self.results = results
            self.present_guardians = present
            self.phase = CLOSED
            self._pending.clear()
            self._clean_verification = None

    def public_record(self) -> PublicElectionRecord:
        """
        Dati pubblici dell'elezione chiusa, per il registro.
        """

        with self._lock:
            if self.phase != CLOSED or self.scrutiny is None:
                raise ValueError("Il registro è disponibile dopo la chiusura.")

            return PublicElectionRecord(
                config=self.config,
                ceremony=self.ceremony,
                board=self.board,
                tallies=self.tallies,
                results=self.results,
                scrutiny=self.scrutiny,
            )

    def registry_json(self, tampering: str | None = None) -> str:
        """
        Registro pubblico in JSON, eventualmente manomesso.

        La manomissione modifica solo il JSON pubblicato: lo stato
        dell'elezione non cambia.
        """

        registry = public_registry_to_json(
            self.public_record(),
            self.params,
        )

        if tampering is None:
            return registry

        if tampering not in TAMPERINGS:
            raise ValueError("Manomissione sconosciuta.")

        data = json.loads(registry)
        TAMPERINGS[tampering][1](data)

        return json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    def verify(self, tampering: str | None = None) -> VerificationReport:
        """
        Esegue il verificatore indipendente sul registro pubblico.

        Il verificatore legge solo il JSON e non importa evoto. L'esito
        sul registro originale viene ricordato, perché dopo la chiusura
        il registro non cambia più.
        """

        if tampering is None and self._clean_verification is not None:
            return self._clean_verification

        registry = self.registry_json(tampering)

        start = time.perf_counter()
        verdict = verify_public_registry(registry)
        seconds = time.perf_counter() - start

        report = VerificationReport(
            tampering=tampering,
            checks={
                name: verdict[name]
                for name in CHECK_DESCRIPTIONS
            },
            overall=verdict["overall"],
            seconds=seconds,
        )

        if tampering is None:
            self._clean_verification = report

        return report

    def counts(self) -> dict[str, int]:
        """
        Numeri della bacheca: schede depositate e sprecate, carte
        d'identità fittizie con cui si è già votato dal browser.
        """

        with self._lock:
            cast = sum(entry.state == CAST for entry in self.board.entries)

            return {
                "cast": cast,
                "spoiled": len(self.board.entries) - cast,
                "demo_voted": sum(
                    code in self.roll.voted
                    for code in self.demo_voters
                ),
            }
