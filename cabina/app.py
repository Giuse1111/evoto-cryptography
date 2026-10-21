"""
Applicazione Flask della cabina elettorale dimostrativa.

Le pagine mostrano soltanto lo stato di DemoHost e di ElectionSession
(preparazione.py e sessione.py): qui non c'è crittografia, solo la
lettura dei moduli e la scelta della pagina da mostrare.

Il menu segue i passi della prova, una voce per pagina:

    /                     panoramica per chi usa la demo
    /prepara              1. come votano gli elettori simulati
    /cabina               2. accesso con il codice dell'elettore
    /cabina/<codice>         scheda della circoscrizione
    /scheda/<token>/...      deposito oppure sfida del dispositivo
    /ricevuta/<token>        codice di tracciamento
    /sfida/<token>           esito della sfida del dispositivo
    /bacheca              3. bacheca pubblica e ricerca del codice
    /spoglio              4. chiusura con i garanti presenti e risultati
    /verifica             5. verificatore indipendente e manomissioni

    /stato                avanzamento della preparazione (JSON)
    /registro.json        registro pubblico da scaricare
"""

from dataclasses import dataclass
from pathlib import Path
import time

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.datastructures import MultiDict

from cabina.preparazione import (
    GUARDIAN_PRESETS,
    MAX_BLANK_PERCENT,
    MAX_SPOIL_PERCENT,
    MODES,
    ORIENTATIONS,
    DemoHost,
    format_duration,
    parse_settings,
    settings_to_form,
)
from cabina.sessione import (
    CHECK_DESCRIPTIONS,
    CLOSED,
    PLAIN_CHECK_DESCRIPTIONS,
    TAMPERINGS,
    ElectionSession,
    format_code,
    normalize_card,
    normalize_code,
)
from evoto.configurazione import coalition_of
from evoto.urna import CAST
from evoto.voto import (
    VoterChoice,
    choice_to_plaintexts,
)


# Pagine sempre raggiungibili, anche mentre si prepara una simulazione.
ALWAYS_OPEN = frozenset({"index", "prepare", "status", "static"})


@dataclass(frozen=True)
class NavStep:
    """
    Voce del menu a passi.

    done: il passo è già stato fatto (segno di spunta).
    locked: il passo non è ancora disponibile (voce grigia, senza link).
    """

    key: str
    label: str
    endpoint: str
    done: bool
    locked: bool
    hint: str


def navigation(host: DemoHost) -> tuple[NavStep, ...]:
    """
    Menu a passi: Prepara, Vota, Bacheca, Spoglio e risultati, Verifica.

    Si comincia sempre da «Prepara»: finché la simulazione non è stata
    scelta lì (preparata o confermata), gli altri passi sono bloccati.
    """

    session = host.session
    available = session is not None and not host.preparing
    ready = available and host.confirmed
    closed = ready and session.phase == CLOSED
    voted = closed or (ready and session.counts()["demo_voted"] > 0)
    verified = closed and session.clean_verification is not None
    waiting = (
        "Prima scegli la simulazione nella pagina Prepara"
        if available
        else "Disponibile quando la simulazione è pronta"
    )

    return (
        NavStep(
            key="prepara",
            label="Prepara",
            endpoint="prepare",
            done=ready,
            locked=False,
            hint="Scegli come votano gli elettori simulati",
        ),
        NavStep(
            key="vota",
            label="Vota",
            endpoint="access",
            done=voted,
            locked=not ready,
            hint="Accedi con un documento fittizio e vota" if ready else waiting,
        ),
        NavStep(
            key="bacheca",
            label="Bacheca",
            endpoint="board",
            done=closed,
            locked=not ready,
            hint="Ritrova la tua scheda" if ready else waiting,
        ),
        NavStep(
            key="spoglio",
            label="Spoglio e risultati",
            endpoint="closing",
            done=closed,
            locked=not ready,
            hint="Chiudi il voto e guarda i risultati" if ready else waiting,
        ),
        NavStep(
            key="verifica",
            label="Verifica",
            endpoint="verification",
            done=verified,
            locked=not closed,
            hint=(
                "Ricontrolla l'elezione"
                if closed
                else "Disponibile dopo lo spoglio"
            ),
        ),
    )


def next_step(host: DemoHost) -> tuple[str, str]:
    """
    Azione consigliata nella panoramica: etichetta del pulsante e pagina.
    """

    session = host.session

    if host.preparing:
        return "Segui la preparazione", "prepare"

    if session is None:
        return "Prepara una simulazione", "prepare"

    if not host.confirmed:
        return "Inizia: prepara la simulazione", "prepare"

    if session.phase != CLOSED:
        if session.counts()["demo_voted"] == 0:
            return "Vai a votare", "access"

        return "Chiudi il voto e fai lo spoglio", "closing"

    if session.clean_verification is None:
        return "Verifica l'elezione", "verification"

    return "Prepara una nuova simulazione", "prepare"


def decimal(value: float, digits: int = 1) -> str:
    """
    Numero con la virgola decimale, come si scrive in italiano.
    """

    return f"{value:.{digits}f}".replace(".", ",")


def parse_choice(form: MultiDict) -> VoterChoice:
    """
    Legge la scelta dal modulo della scheda.

    lista vale l'indice della lista oppure "bianca"; preferenze sono
    gli indici delle caselle di preferenza segnate.
    """

    raw_list = form.get("lista", "")

    try:
        preferences = tuple(
            sorted(int(value) for value in form.getlist("preferenze"))
        )
        list_index = int(raw_list) if raw_list not in ("", "bianca") else None
    except ValueError as error:
        raise ValueError("Il modulo della scheda non è valido.") from error

    if raw_list == "bianca":
        if preferences:
            raise ValueError("Una scheda bianca non può avere preferenze.")

        return VoterChoice(list_index=None)

    if list_index is None and not preferences:
        raise ValueError("Segna una lista oppure la scheda bianca.")

    return VoterChoice(
        list_index=list_index,
        preferences=preferences,
    )


def parse_guardians(form: MultiDict) -> tuple[int, ...]:
    """
    Garanti presenti segnati nel modulo dello spoglio.
    """

    try:
        return tuple(int(value) for value in form.getlist("garanti"))
    except ValueError as error:
        raise ValueError("Scegli i garanti presenti dall'elenco.") from error


def create_app(
    host: DemoHost | ElectionSession,
    registry_path: Path | None = None,
) -> Flask:
    """
    Crea l'applicazione web.

    host è il DemoHost che conserva l'elezione e ne prepara di nuove;
    per comodità si può passare anche una sola ElectionSession già
    pronta. Se registry_path è indicato, alla chiusura il registro
    pubblico viene scritto anche in quel file, per lanciare il
    verificatore dal terminale.
    """

    if isinstance(host, ElectionSession):
        host = DemoHost(host.config, session=host)

    app = Flask(__name__)
    app.config["HOST"] = host
    app.add_template_filter(format_duration, "duration")
    app.add_template_filter(decimal, "decimal")

    @app.before_request
    def load_session():
        # La sessione si legge una volta per richiesta: se intanto una
        # nuova simulazione la sostituisce, la pagina resta coerente.
        g.session = host.session
        g.available = g.session is not None and not host.preparing
        g.ready = g.available and host.confirmed

        if request.endpoint in ALWAYS_OPEN or request.endpoint is None:
            return None

        if not g.ready:
            return redirect(url_for("prepare"))

        return None

    @app.context_processor
    def common_values() -> dict[str, object]:
        session = g.get("session")
        ready = g.get("ready", False)
        available = g.get("available", False)

        def code(value: int, short: bool = False) -> str:
            return format_code(value, session.params, short=short)

        return {
            "host": host,
            "election": session,
            "config": host.config,
            "mode": host.mode,
            "ready": ready,
            "available": available,
            "unconfirmed": available and not ready,
            "preparing": host.preparing,
            "closed": ready and session.phase == CLOSED,
            "steps": navigation(host),
            "code": code,
            "CAST": CAST,
        }

    def error_page(message: str, back: str | None = None, status: int = 400):
        return (
            render_template(
                "errore.html",
                message=message,
                back=back,
            ),
            status,
        )

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            active="home",
            counts=g.session.counts() if g.available else None,
            next_step=next_step(host),
            status=host.status,
        )

    @app.route("/prepara", methods=["GET", "POST"])
    def prepare():
        error = None
        values = settings_to_form(host.form_defaults())

        if request.method == "POST" and request.form.get("azione") == "usa":
            # La simulazione già pronta va bene così: si passa al voto.
            try:
                host.confirm()
            except ValueError as exception:
                error = str(exception)
            else:
                return redirect(url_for("access"))
        elif request.method == "POST":
            values.update(request.form.to_dict())

            try:
                host.prepare(parse_settings(request.form))
            except ValueError as exception:
                error = str(exception)
            else:
                return redirect(url_for("prepare", pronta=1))

        status = host.status

        if status.running:
            return render_template(
                "preparazione.html",
                active="prepara",
                modes=MODES,
                status=status,
                remaining=status.remaining_seconds(time.time()),
            )

        try:
            estimate = host.estimate_seconds(parse_settings(values))
        except ValueError:
            estimate = None

        guardian_options = [
            (f"{count}-{quorum}", f"{count} garanti, ne servono {quorum}")
            for count, quorum in GUARDIAN_PRESETS
        ]

        if values["garanti"] not in dict(guardian_options):
            count, _, quorum = values["garanti"].partition("-")
            guardian_options.append(
                (values["garanti"], f"{count} garanti, ne servono {quorum}")
            )

        return render_template(
            "prepara.html",
            active="prepara",
            error=error,
            failed=status.error,
            ready_now=request.args.get("pronta") == "1" and g.ready,
            values=values,
            estimate=estimate,
            modes=MODES,
            orientations=ORIENTATIONS,
            guardian_options=guardian_options,
            single_lists=[
                name
                for index, name in enumerate(host.config.list_names)
                if coalition_of(host.config, index) is None
            ],
            max_blank=MAX_BLANK_PERCENT,
            max_spoil=MAX_SPOIL_PERCENT,
            speeds={key: host.seconds_per_ballot(key) for key in MODES},
            counts=g.session.counts() if g.available else None,
        )

    @app.get("/stato")
    def status():
        current = host.status
        now = time.time()

        return jsonify(
            phase=current.phase,
            running=current.running,
            done=current.done,
            total=current.total,
            elapsed=round(now - current.started, 1) if current.running else 0,
            remaining=current.remaining_seconds(now),
            error=current.error,
        )

    @app.route("/cabina", methods=["GET", "POST"])
    def access():
        session = g.session
        error = None
        typed = ""

        if request.method == "POST":
            typed = normalize_card(request.form.get("codice", ""))

            try:
                session.voter_district(typed)
            except ValueError as exception:
                error = str(exception)
            else:
                return redirect(url_for("ballot", voter_id=typed))

        codes: dict[int, list[str]] = {}

        for voter_id, district in session.available_demo_codes().items():
            codes.setdefault(district, []).append(voter_id)

        return render_template(
            "accesso.html",
            active="vota",
            codes=codes,
            error=error,
            typed=typed,
        )

    @app.route("/cabina/<voter_id>", methods=["GET", "POST"])
    def ballot(voter_id: str):
        session = g.session
        normalized = normalize_card(voter_id)

        if request.method == "GET" and normalized and normalized != voter_id:
            return redirect(url_for("ballot", voter_id=normalized))

        try:
            district = session.voter_district(normalized)
        except ValueError as exception:
            return error_page(str(exception), url_for("access"))

        error = None

        if request.method == "POST":
            try:
                choice = parse_choice(request.form)
                pending = session.prepare(normalized, choice)
            except ValueError as exception:
                error = str(exception)
            else:
                layout = session.layouts[district]

                return render_template(
                    "conferma.html",
                    active="vota",
                    pending=pending,
                    district=session.config.districts[district],
                    description=session.describe_choice(
                        district,
                        *choice_to_plaintexts(layout, choice),
                    ),
                    ciphertexts=len(layout.preference_metadata)
                    + layout.list_count
                    + 1,
                    proofs=len(pending.prepared.proofs.r1_proofs)
                    + len(pending.prepared.proofs.r3_proofs)
                    + len(pending.prepared.proofs.r5_proofs)
                    + 2,
                )

        # Dopo un errore la scheda torna con le scelte già fatte.
        form = request.form if request.method == "POST" else MultiDict()

        return render_template(
            "scheda.html",
            active="vota",
            voter_id=normalized,
            district=session.config.districts[district],
            layout=session.layouts[district],
            groups=session.ballot_view(district),
            error=error,
            chosen_list=form.get("lista", ""),
            chosen_preferences=set(form.getlist("preferenze")),
            faulty=bool(form.get("scorretto")),
        )

    @app.post("/scheda/<token>/deposita")
    def cast(token: str):
        try:
            g.session.cast(token)
        except ValueError as exception:
            return error_page(str(exception), url_for("access"))

        return redirect(url_for("receipt", token=token))

    @app.get("/ricevuta/<token>")
    def receipt(token: str):
        try:
            entry = g.session.receipt(token)
        except ValueError as exception:
            return error_page(str(exception), url_for("access"), status=404)

        return render_template(
            "ricevuta.html",
            active="vota",
            entry=entry,
            district=g.session.config.districts[entry.district_index],
        )

    @app.post("/scheda/<token>/sfida")
    def spoil(token: str):
        try:
            g.session.spoil(token)
        except ValueError as exception:
            return error_page(str(exception), url_for("access"))

        return redirect(url_for("challenge", token=token))

    @app.get("/sfida/<token>")
    def challenge(token: str):
        try:
            report = g.session.spoil_report(token)
        except ValueError as exception:
            return error_page(str(exception), url_for("access"), status=404)

        return render_template(
            "sfida.html",
            active="vota",
            report=report,
            can_vote=report.voter_id in g.session.available_demo_codes(),
        )

    @app.get("/bacheca")
    def board():
        session = g.session
        search = request.args.get("codice", "").strip()
        too_short = (
            bool(search)
            and len(normalize_code(search)) < session.min_search_length
        )
        found = session.find_entries(search) if search and not too_short else ()

        return render_template(
            "bacheca.html",
            active="bacheca",
            entries=session.board.entries,
            counts=session.counts(),
            board_is_valid=session.board_is_valid(),
            search=search,
            too_short=too_short,
            found=sorted(entry.sequence for entry in found),
        )

    @app.route("/spoglio", methods=["GET", "POST"])
    def closing():
        session = g.session

        if session.phase == CLOSED:
            return render_template(
                "risultati.html",
                active="spoglio",
                absent=tuple(
                    index
                    for index in range(1, session.guardian_count + 1)
                    if index not in session.present_guardians
                ),
            )

        error = None
        selected = session.default_present_guardians()

        if request.method == "POST":
            try:
                selected = parse_guardians(request.form)

                if not request.form.get("conferma"):
                    raise ValueError(
                        "Spunta la conferma: dopo la chiusura non si può "
                        "più votare."
                    )

                session.close(selected)
            except ValueError as exception:
                error = str(exception)
            else:
                write_registry(session)

                return redirect(url_for("closing"))

        return render_template(
            "spoglio.html",
            active="spoglio",
            counts=session.counts(),
            selected=selected,
            error=error,
        )

    def write_registry(session: ElectionSession) -> None:
        """
        Scrive il registro pubblico nel file indicato all'avvio. Se non
        si può scrivere, la pagina lo segnala ma l'elezione resta
        chiusa e il registro si può sempre scaricare.
        """

        if registry_path is None:
            return

        try:
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            registry_path.write_text(
                session.registry_json(),
                encoding="utf-8",
            )
        except OSError as exception:
            host.registry_warning = (
                f"Il registro non è stato scritto in {registry_path}: "
                f"{exception.strerror or exception}. Puoi comunque "
                "scaricarlo da questa pagina."
            )
        else:
            host.registry_warning = None

    @app.get("/risultati")
    def results():
        # Vecchio indirizzo: i risultati ora sono nella pagina dello spoglio.
        return redirect(url_for("closing"))

    @app.route("/verifica", methods=["GET", "POST"])
    def verification():
        session = g.session
        report = None
        error = None

        if request.method == "POST" and session.phase == CLOSED:
            tampering = request.form.get("manomissione") or None

            try:
                report = session.verify(tampering)
            except ValueError as exception:
                error = str(exception)

        return render_template(
            "verifica.html",
            active="verifica",
            report=report,
            error=error,
            tamperings=TAMPERINGS,
            descriptions=CHECK_DESCRIPTIONS,
            plain=PLAIN_CHECK_DESCRIPTIONS,
            registry_path=registry_path,
            estimate=host.estimate_verification_seconds(),
        )

    @app.get("/registro.json")
    def registry():
        tampering = request.args.get("manomissione") or None

        try:
            content = g.session.registry_json(tampering)
        except ValueError as exception:
            return error_page(str(exception), url_for("verification"))

        name = (
            "registro.json"
            if tampering is None
            else f"registro_manomesso_{tampering}.json"
        )

        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename={name}"},
        )

    return app
