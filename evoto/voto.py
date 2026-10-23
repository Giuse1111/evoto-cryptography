"""
Il voto dal lato dell'elettore nel progetto evoto.

Questo modulo contiene la logica del dispositivo di voto:
- la scelta dell'elettore (lista, scheda bianca, preferenze);
- la sua traduzione nei bit delle caselle della scheda;
- la cifratura della scheda con le prove R1-R5;
- l'impronta mostrata all'elettore prima del deposito.

La cabina web userà queste funzioni; qui non c'è interfaccia.
Segue la specifica condivisa docs/spec_f1.md, sezione 43.
"""

from dataclasses import dataclass
import secrets

from evoto.elgamal import encrypt
from evoto.gruppo import GroupParameters
from evoto.scheda import (
    BallotLayout,
    BallotProofs,
    BallotWitness,
    EncryptedBallot,
    prove_ballot,
)
from evoto.urna import compute_ballot_hash


@dataclass(frozen=True)
class VoterChoice:
    """
    Scelta dell'elettore.

    list_index: lista votata, oppure None per nessuna lista.
    preferences: indici delle caselle di preferenza segnate,
    nell'ordine del layout della circoscrizione.

    Senza lista e senza preferenze la scheda è bianca.
    """

    list_index: int | None
    preferences: tuple[int, ...] = ()


@dataclass(frozen=True)
class PreparedBallot:
    """
    Scheda cifrata pronta per il deposito o per la sfida.

    ballot, proofs e ballot_hash sono pubblici.

    witness contiene voti e nonce: resta nel dispositivo e viene
    rivelato solo se l'elettore sceglie di sprecare la scheda.
    """

    district_index: int
    ballot: EncryptedBallot
    proofs: BallotProofs
    witness: BallotWitness
    ballot_hash: int


def apply_gender_rule(
    layout: BallotLayout,
    preferences: tuple[int, ...],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """
    Applica la regola di genere della legge alle preferenze segnate.

    Se le preferenze sono più di una devono riguardare candidati di
    genere diverso; altrimenti la seconda e la terza, nell'ordine di
    lista, sono annullate (legge 9 ottobre 2026, n. 180, art. 1,
    comma 4). Le caselle di una lista seguono l'ordine dei candidati,
    quindi l'ordine di lista è quello degli indici.

    Restituisce le preferenze che restano e quelle annullate. Il
    dispositivo applica la regola prima di cifrare, come farebbe lo
    scrutatore con una scheda di carta; la prova R5 garantisce poi che
    la scheda cifrata la rispetti.
    """

    ordered = tuple(sorted(preferences))

    if len(ordered) < 2:
        return ordered, ()

    genders = {
        layout.preference_metadata[preference].gender
        for preference in ordered
    }

    if len(genders) > 1:
        return ordered, ()

    return ordered[:1], ordered[1:]


def choice_to_plaintexts(
    layout: BallotLayout,
    choice: VoterChoice,
) -> tuple[tuple[int, ...], int, tuple[int, ...]]:
    """
    Traduce la scelta nei bit delle caselle: liste, bianca, preferenze.

    Come in molte elezioni italiane, una preferenza espressa senza
    segnare la lista vale anche per la lista: se tutte le preferenze
    appartengono alla stessa lista, il dispositivo segna quella lista.

    Le preferenze tutte dello stesso genere vengono ridotte alla prima
    in ordine di lista (apply_gender_rule), come prevede la legge.

    Per il resto controlliamo solo la forma della scelta. Il rispetto
    delle regole (preferenze nella lista votata, al massimo tre, regola
    di genere) è garantito dalle prove: una scelta che le viola non
    riesce a produrre una scheda valida.
    """

    preference_count = len(layout.preference_metadata)

    if len(set(choice.preferences)) != len(choice.preferences):
        raise ValueError("Una preferenza non può essere segnata due volte.")

    for preference in choice.preferences:
        if not 0 <= preference < preference_count:
            raise ValueError("La casella di preferenza non esiste.")

    preferences, _ = apply_gender_rule(
        layout,
        choice.preferences,
    )

    list_index = choice.list_index

    if list_index is None and choice.preferences:
        preference_lists = {
            layout.preference_metadata[preference].list_index
            for preference in choice.preferences
        }

        if len(preference_lists) != 1:
            raise ValueError(
                "Preferenze in liste diverse senza una lista segnata."
            )

        list_index = preference_lists.pop()

    if list_index is not None and not 0 <= list_index < layout.list_count:
        raise ValueError("La lista scelta non esiste.")

    list_plaintexts = tuple(
        1 if index == list_index else 0
        for index in range(layout.list_count)
    )

    blank_plaintext = 1 if list_index is None else 0

    preference_plaintexts = tuple(
        1 if index in preferences else 0
        for index in range(preference_count)
    )

    return list_plaintexts, blank_plaintext, preference_plaintexts


def prepare_ballot(
    layout: BallotLayout,
    district_index: int,
    choice: VoterChoice,
    public_key: int,
    params: GroupParameters,
    extended_base_hash: int,
    nonces: tuple[int, ...] | None = None,
) -> PreparedBallot:
    """
    Cifra la scelta dell'elettore e costruisce le prove R1-R5.

    Ogni casella viene cifrata con un nonce fresco compreso tra 1 e q - 1.
    I nonce possono essere passati esplicitamente nei test, nell'ordine
    canonico: liste, scheda bianca, preferenze.

    Se la scelta viola una regola della scheda, la costruzione delle
    prove fallisce con un ValueError.
    """

    list_plaintexts, blank_plaintext, preference_plaintexts = (
        choice_to_plaintexts(layout, choice)
    )

    plaintexts = list_plaintexts + (blank_plaintext,) + preference_plaintexts

    if nonces is None:
        nonces = tuple(
            secrets.randbelow(params.q - 1) + 1
            for _ in plaintexts
        )

    if len(nonces) != len(plaintexts):
        raise ValueError("Serve un nonce per ogni casella della scheda.")

    ciphertexts = tuple(
        encrypt(
            message=plaintext,
            public_key=public_key,
            params=params,
            nonce=nonce,
        )
        for plaintext, nonce in zip(plaintexts, nonces, strict=True)
    )

    list_count = layout.list_count

    ballot = EncryptedBallot(
        list_ciphertexts=ciphertexts[:list_count],
        blank_ciphertext=ciphertexts[list_count],
        preference_ciphertexts=ciphertexts[list_count + 1:],
    )

    witness = BallotWitness(
        list_plaintexts=list_plaintexts,
        blank_plaintext=blank_plaintext,
        preference_plaintexts=preference_plaintexts,
        list_nonces=tuple(nonces[:list_count]),
        blank_nonce=nonces[list_count],
        preference_nonces=tuple(nonces[list_count + 1:]),
    )

    proofs = prove_ballot(
        layout=layout,
        ballot=ballot,
        witness=witness,
        public_key=public_key,
        params=params,
        context=extended_base_hash,
    )

    ballot_hash = compute_ballot_hash(
        ballot,
        district_index,
        extended_base_hash,
        params,
    )

    return PreparedBallot(
        district_index=district_index,
        ballot=ballot,
        proofs=proofs,
        witness=witness,
        ballot_hash=ballot_hash,
    )
