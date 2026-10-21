"""
Preparazione delle simulazioni della cabina.

La pagina «Prepara la simulazione» permette di scegliere come votano gli
elettori simulati dell'elezione di esempio: quanti sono, verso quali liste
tendono, quante schede bianche e quante sfide del dispositivo, quanti
garanti e con quale gruppo crittografico. La struttura dell'elezione
(liste, coalizioni, candidati) resta quella del file di configurazione.

La preparazione (cerimonia delle chiavi e voto degli elettori simulati)
gira in un thread separato, così la pagina può mostrare l'avanzamento.
Il server serve una sola elezione alla volta: DemoHost la conserva e la
sostituisce quando una nuova simulazione è pronta.

Il sito comincia sempre da «Prepara»: la simulazione preparata all'avvio
del server va confermata con un clic (oppure sostituita) prima di poter
votare, così chi usa la demo non salta il primo passo.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
import random
import threading
import time

from cabina.sessione import ElectionSession
from evoto.configurazione import ElectionConfig
from evoto.gruppo import (
    DEMO_PARAMS,
    TEST_PARAMS,
    GroupParameters,
)


@dataclass(frozen=True)
class Mode:
    """
    Modalità della simulazione: il gruppo crittografico usato.

    max_voters_per_district tiene la preparazione entro un paio di
    minuti; seconds_per_ballot è la stima iniziale del tempo di una
    scheda, sostituita dalla misura dell'ultima simulazione.

    verify_seconds_per_entry e verify_fixed_seconds stimano la durata
    del verificatore indipendente: un tempo per ogni riga della bacheca
    più le prove dei garanti e delle share, che non dipendono dal
    numero di schede.
    """

    key: str
    label: str
    description: str
    params: GroupParameters
    max_voters_per_district: int
    seconds_per_ballot: float
    verify_seconds_per_entry: float
    verify_fixed_seconds: float


MODES = {
    "realistica": Mode(
        key="realistica",
        label="Realistica",
        description=(
            "Numeri da 2048 bit, come in un sistema vero. Ogni scheda "
            "richiede circa un secondo."
        ),
        params=DEMO_PARAMS,
        max_voters_per_district=20,
        seconds_per_ballot=0.9,
        verify_seconds_per_entry=0.45,
        verify_fixed_seconds=3.0,
    ),
    "veloce": Mode(
        key="veloce",
        label="Veloce",
        description=(
            "Numeri piccoli, adatti a provare con molti elettori: tutto è "
            "istantaneo, ma questi numeri non proteggono davvero il voto."
        ),
        params=TEST_PARAMS,
        max_voters_per_district=200,
        seconds_per_ballot=0.01,
        verify_seconds_per_entry=0.002,
        verify_fixed_seconds=0.0,
    ),
}


def mode_of(params: GroupParameters) -> Mode | None:
    """
    Modalità che usa questo gruppo, oppure None per un gruppo diverso.
    """

    for mode in MODES.values():
        if mode.params == params:
            return mode

    return None


@dataclass(frozen=True)
class Orientation:
    """
    Tendenza di voto degli elettori simulati.
    """

    key: str
    label: str
    description: str


ORIENTATIONS = {
    "in_testa": Orientation(
        key="in_testa",
        label="Una coalizione in testa",
        description=(
            "La prima coalizione prende circa il 47% dei voti: di solito "
            "supera la soglia e vince il premio di maggioranza."
        ),
    ),
    "testa_a_testa": Orientation(
        key="testa_a_testa",
        label="Testa a testa",
        description=(
            "Le due coalizioni principali hanno quasi gli stessi voti: "
            "di solito nessuna arriva al premio."
        ),
    ),
    "equilibrato": Orientation(
        key="equilibrato",
        label="Tutte le liste alla pari",
        description="Ogni lista ha la stessa probabilità di essere scelta.",
    ),
    "casuale": Orientation(
        key="casuale",
        label="A sorpresa",
        description=(
            "Ogni lista riceve un peso casuale, diverso per ogni seme: "
            "il risultato non si può prevedere."
        ),
    ),
}

# Garanti e quorum proposti nella pagina.
GUARDIAN_PRESETS = (
    (3, 2),
    (5, 3),
    (7, 4),
)

MAX_BLANK_PERCENT = 20
MAX_SPOIL_PERCENT = 30


@dataclass(frozen=True)
class SimulationSettings:
    """
    Scelte della pagina «Prepara la simulazione».

    Le percentuali sono intere (3 vuol dire 3%).
    """

    mode: str = "realistica"
    voters_per_district: int = 10
    orientation: str = "in_testa"
    blank_percent: int = 3
    spoil_percent: int = 5
    guardian_count: int = 5
    quorum: int = 3
    seed: int = 2026

    def validate(self) -> None:
        """
        Controlla che le scelte siano ammesse; solleva ValueError con un
        messaggio da mostrare all'utente.
        """

        if self.mode not in MODES:
            raise ValueError("Scegli la modalità realistica o veloce.")

        mode = MODES[self.mode]

        if not 0 <= self.voters_per_district <= mode.max_voters_per_district:
            raise ValueError(
                f"In modalità {mode.label.lower()} gli elettori simulati per "
                f"circoscrizione vanno da 0 a {mode.max_voters_per_district}."
            )

        if self.orientation not in ORIENTATIONS:
            raise ValueError("Scegli una tendenza di voto dall'elenco.")

        if not 0 <= self.blank_percent <= MAX_BLANK_PERCENT:
            raise ValueError(
                f"Le schede bianche vanno da 0 a {MAX_BLANK_PERCENT}%."
            )

        if not 0 <= self.spoil_percent <= MAX_SPOIL_PERCENT:
            raise ValueError(
                f"Le sfide del dispositivo vanno da 0 a {MAX_SPOIL_PERCENT}%."
            )

        if not 1 <= self.quorum <= self.guardian_count <= 9:
            raise ValueError(
                "Servono da 1 a 9 garanti, e il quorum non può superarli."
            )

        if self.seed < 0:
            raise ValueError("Il seme deve essere un numero non negativo.")


def parse_settings(form) -> SimulationSettings:
    """
    Legge le scelte dal modulo della pagina; solleva ValueError con un
    messaggio da mostrare se un campo non è valido.
    """

    try:
        guardians, quorum = (
            int(part)
            for part in form.get("garanti", "5-3").split("-")
        )

        settings = SimulationSettings(
            mode=form.get("modalita", "realistica"),
            voters_per_district=int(form.get("elettori", "10")),
            orientation=form.get("tendenza", "in_testa"),
            blank_percent=int(form.get("bianche", "3")),
            spoil_percent=int(form.get("sfide", "5")),
            guardian_count=guardians,
            quorum=quorum,
            seed=int(form.get("seme") or "2026"),
        )
    except ValueError as error:
        raise ValueError(
            "Controlla i valori del modulo: servono numeri interi."
        ) from error

    settings.validate()

    return settings


def settings_to_form(settings: SimulationSettings) -> dict[str, str]:
    """
    Valori del modulo che corrispondono alle scelte indicate: servono a
    riempire la pagina con la simulazione attuale.
    """

    return {
        "modalita": settings.mode,
        "elettori": str(settings.voters_per_district),
        "tendenza": settings.orientation,
        "bianche": str(settings.blank_percent),
        "sfide": str(settings.spoil_percent),
        "garanti": f"{settings.guardian_count}-{settings.quorum}",
        "seme": str(settings.seed),
    }


def format_duration(seconds: float) -> str:
    """
    Durata scritta a parole, arrotondata: «circa 30 secondi».
    """

    if seconds < 1:
        return "meno di un secondo"

    if seconds < 1.5:
        return "circa un secondo"

    if seconds < 60:
        return f"circa {round(seconds)} secondi"

    if seconds < 90:
        return "circa un minuto"

    return f"circa {round(seconds / 60)} minuti"


def orientation_weights(
    config: ElectionConfig,
    orientation: str,
    seed: int,
) -> tuple[float, ...]:
    """
    Pesi delle liste per la tendenza di voto scelta.

    - in_testa: le liste della prima coalizione hanno insieme il 47%,
      le altre si dividono il resto in parti uguali;
    - testa_a_testa: le prime due coalizioni hanno il 37% ciascuna;
    - equilibrato: pesi tutti uguali;
    - casuale: pesi casuali tra 0,5 e 3, fissati dal seme.

    Senza coalizioni si usano le prime liste al loro posto.
    """

    count = len(config.list_names)

    if orientation == "equilibrato":
        return tuple(1.0 for _ in range(count))

    if orientation == "casuale":
        rng = random.Random(seed)

        return tuple(rng.uniform(0.5, 3.0) for _ in range(count))

    groups = [coalition.list_indices for coalition in config.coalitions]

    # Le liste non coalizzate fanno da gruppi singoli, dopo le coalizioni.
    grouped = {index for group in groups for index in group}
    groups += [(index,) for index in range(count) if index not in grouped]

    if orientation == "in_testa":
        shares = [(groups[0], 0.47)]
    elif orientation == "testa_a_testa":
        if len(groups) < 2:
            return tuple(1.0 for _ in range(count))

        shares = [(groups[0], 0.37), (groups[1], 0.37)]
    else:
        raise ValueError("Tendenza di voto sconosciuta.")

    weights = [0.0] * count

    for indices, share in shares:
        for index in indices:
            weights[index] = share / len(indices)

    assigned = {index for indices, _ in shares for index in indices}
    others = [index for index in range(count) if index not in assigned]
    remaining = 1.0 - sum(share for _, share in shares)

    for index in others:
        weights[index] = remaining / len(others)

    return tuple(weights)


@dataclass(frozen=True)
class PreparationStatus:
    """
    Avanzamento della preparazione di una simulazione.

    phase: "pronta", "cerimonia", "voto" oppure "errore".
    done, total: elettori simulati che hanno votato e da far votare.
    started: istante di inizio (time.time()).
    estimate: durata stimata all'inizio, in secondi.
    settings: scelte della simulazione in preparazione.
    """

    phase: str = "pronta"
    done: int = 0
    total: int = 0
    started: float = 0.0
    estimate: float = 0.0
    settings: SimulationSettings | None = None
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.phase in ("cerimonia", "voto")

    def remaining_seconds(self, now: float) -> float | None:
        """
        Secondi che mancano alla fine: dalla velocità misurata finora,
        oppure dalla stima iniziale se nessun elettore ha ancora votato.
        """

        if not self.running:
            return None

        elapsed = max(now - self.started, 0.0)

        if self.done:
            return elapsed / self.done * (self.total - self.done)

        return max(self.estimate - elapsed, 0.0)


class DemoHost:
    """
    Contiene l'elezione servita dalla cabina e ne prepara di nuove.

    Mentre una nuova simulazione si prepara, il server mostra solo la
    pagina di avanzamento; quando è pronta, sostituisce la precedente.

    confirmed indica che la simulazione attuale è stata scelta dalla
    pagina «Prepara», preparandola oppure confermando quella già pronta:
    finché è False le pagine del voto restano bloccate.
    """

    def __init__(
        self,
        config: ElectionConfig,
        session: ElectionSession | None = None,
        settings: SimulationSettings | None = None,
        confirmed: bool = False,
    ) -> None:
        self.config = config
        self.session = session
        self.confirmed = confirmed and session is not None
        self.registry_warning: str | None = None

        # Scelte della simulazione attuale: None se la sessione è stata
        # preparata altrove e non si sa come hanno votato gli elettori.
        self.settings = settings

        self._lock = threading.Lock()
        self._status = PreparationStatus()
        self._seconds_per_ballot = {
            key: mode.seconds_per_ballot
            for key, mode in MODES.items()
        }

        if session is not None:
            self._remember_speed(session)

    @property
    def status(self) -> PreparationStatus:
        with self._lock:
            return self._status

    @property
    def preparing(self) -> bool:
        return self.status.running

    @property
    def mode(self) -> Mode | None:
        """
        Modalità della simulazione attuale.
        """

        session = self.session

        return None if session is None else mode_of(session.params)

    @property
    def ready(self) -> bool:
        """
        C'è una simulazione pronta e confermata: si può votare.
        """

        return self.session is not None and self.confirmed and not self.preparing

    def confirm(self) -> None:
        """
        Conferma la simulazione già pronta, senza prepararne un'altra.
        """

        with self._lock:
            if self._status.running:
                raise ValueError("Una simulazione è già in preparazione.")

            if self.session is None:
                raise ValueError("Non c'è ancora una simulazione da confermare.")

            self.confirmed = True

    def form_defaults(self) -> SimulationSettings:
        """
        Scelte proposte nella pagina «Prepara la simulazione»: quelle
        della simulazione attuale, oppure i valori di default con la
        modalità e i garanti della sessione.
        """

        if self.settings is not None:
            return self.settings

        session = self.session
        mode = None if session is None else mode_of(session.params)

        if session is None or mode is None:
            return SimulationSettings()

        return SimulationSettings(
            mode=mode.key,
            guardian_count=session.guardian_count,
            quorum=session.quorum,
        )

    def _remember_speed(self, session: ElectionSession) -> None:
        mode = mode_of(session.params)

        if mode is not None and session.seconds_per_ballot is not None:
            self._seconds_per_ballot[mode.key] = session.seconds_per_ballot

    def seconds_per_ballot(self, mode: str) -> float:
        """
        Tempo stimato di una scheda simulata nella modalità indicata.
        """

        return self._seconds_per_ballot[mode]

    def estimate_seconds(self, settings: SimulationSettings) -> float:
        """
        Durata stimata della preparazione: le schede depositate più
        quelle sprecate per le sfide.
        """

        ballots = (
            settings.voters_per_district
            * len(self.config.districts)
            * (1 + settings.spoil_percent / 100)
        )

        return ballots * self._seconds_per_ballot[settings.mode]

    def estimate_verification_seconds(self) -> float | None:
        """
        Durata stimata del verificatore sul registro dell'elezione
        attuale: il tempo dell'ultima verifica, se c'è già stata,
        altrimenti la stima della modalità.
        """

        session = self.session
        mode = self.mode

        if session is None:
            return None

        if session.clean_verification is not None:
            return session.clean_verification.seconds

        if mode is None:
            return None

        return (
            mode.verify_fixed_seconds
            + mode.verify_seconds_per_entry * len(session.board.entries)
        )

    def prepare(
        self,
        settings: SimulationSettings,
        background: bool = True,
        progress: Callable[[int, int], None] | None = None,
        confirm: bool = True,
    ) -> None:
        """
        Prepara una nuova simulazione con le scelte indicate.

        Con background=False lavora nel thread chiamante (lo usano la
        riga di comando all'avvio e i test). progress, se indicato,
        riceve anche lui l'avanzamento. Con confirm=False la simulazione
        pronta va ancora confermata dalla pagina «Prepara»: è il caso
        della simulazione preparata all'avvio del server.
        """

        settings.validate()

        with self._lock:
            if self._status.running:
                raise ValueError("Una simulazione è già in preparazione.")

            self._status = PreparationStatus(
                phase="cerimonia",
                total=settings.voters_per_district * len(self.config.districts),
                started=time.time(),
                estimate=self.estimate_seconds(settings),
                settings=settings,
            )

        if background:
            threading.Thread(
                target=self._run,
                args=(settings, progress, confirm),
                daemon=True,
            ).start()
        else:
            self._run(settings, progress, confirm)

    def _progress(self, done: int, total: int) -> None:
        with self._lock:
            self._status = replace(
                self._status,
                phase="voto",
                done=done,
                total=total,
            )

    def _run(
        self,
        settings: SimulationSettings,
        progress: Callable[[int, int], None] | None,
        confirm: bool,
    ) -> None:
        def report(done: int, total: int) -> None:
            self._progress(done, total)

            if progress is not None:
                progress(done, total)

        try:
            mode = MODES[settings.mode]

            session = ElectionSession(
                config=self.config,
                params=mode.params,
                guardian_count=settings.guardian_count,
                quorum=settings.quorum,
            )

            self._progress(0, self.status.total)

            session.prefill(
                voters_per_district=settings.voters_per_district,
                seed=settings.seed,
                progress=report,
                list_weights=orientation_weights(
                    self.config,
                    settings.orientation,
                    settings.seed,
                ),
                blank_probability=settings.blank_percent / 100,
                spoil_probability=settings.spoil_percent / 100,
            )
        except Exception as error:  # noqa: BLE001 - mostrato nella pagina
            with self._lock:
                self._status = replace(
                    self._status,
                    phase="errore",
                    error=str(error) or error.__class__.__name__,
                )
            return

        with self._lock:
            self._remember_speed(session)
            self.session = session
            self.settings = settings
            self.confirmed = confirm
            self.registry_warning = None
            self._status = PreparationStatus()
