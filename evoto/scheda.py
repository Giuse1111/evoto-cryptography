"""
Modello della scheda politica del progetto evoto.

Questo modulo contiene:
- la struttura pubblica della scheda elettorale;
- la rappresentazione della scheda cifrata;
- i dati privati necessari alla generazione delle prove;
- le prove di validità R1-R5;
- la verifica crittografica delle regole della scheda.

Le primitive crittografiche sono riutilizzate dai moduli
elgamal.py e prove.py, senza ridefinirle.
"""

from dataclasses import dataclass

from evoto.elgamal import (
    Ciphertext,
    divide_ciphertexts,
    multiply_ciphertexts,
)
from evoto.gruppo import (
    GroupParameters,
    is_subgroup_element,
)
from evoto.prove import (
    ValueSetProof,
    prove_value_in_set,
    verify_value_in_set,
)


@dataclass(frozen=True)
class PreferenceMetadata:
    """
    Descrive una casella di preferenza della scheda.

    list_index indica la lista a cui appartiene il candidato.
    gender identifica il genere usato dalla regola di genere R5.
    """

    list_index: int
    gender: str


# Regola di genere (legge 9 ottobre 2026, n. 180, art. 1, comma 4):
# se le preferenze sono più di una devono riguardare candidati di sesso
# diverso, altrimenti la seconda e la terza, nell'ordine di lista, sono
# annullate. Il dispositivo applica l'annullamento prima di cifrare
# (voto.py); la prova R5 dimostra che la scheda cifrata rispetta la
# regola, cioè che le preferenze dei due generi differiscono al più di 1.
# Con al massimo tre preferenze le due condizioni coincidono.
MAX_GENDERS = 2
MAX_PREFERENCES_WITH_GENDER_RULE = 3

# Valori ammessi per il cifrato derivato di R5, che contiene
# (preferenze del primo genere) - (preferenze del secondo) + 1.
R5_ALLOWED_VALUES = (0, 1, 2)


@dataclass(frozen=True)
class BallotLayout:
    """
    Descrive la struttura pubblica della scheda elettorale.

    I metadati e il limite delle preferenze derivano
    dalla configurazione ufficiale dell'elezione
    e non possono essere scelti dal votante.
    """

    list_count: int
    preference_metadata: tuple[PreferenceMetadata, ...]
    max_preferences: int

    def __post_init__(self) -> None:
        """
        Verifica la coerenza strutturale della configurazione.
        """

        if self.list_count < 1:
            raise ValueError(
                "La scheda deve contenere almeno una lista."
            )

        if self.max_preferences < 0:
            raise ValueError(
                "Il numero massimo di preferenze non può essere negativo."
            )

        if self.max_preferences > MAX_PREFERENCES_WITH_GENDER_RULE:
            raise ValueError(
                "Il numero massimo di preferenze non può superare "
                f"{MAX_PREFERENCES_WITH_GENDER_RULE}: la regola di genere "
                "è definita per al massimo tre preferenze."
            )

        genders = {
            metadata.gender
            for metadata in self.preference_metadata
        }

        if len(genders) > MAX_GENDERS:
            raise ValueError(
                "La regola di genere confronta al massimo due generi."
            )

        for metadata in self.preference_metadata:
            if not 0 <= metadata.list_index < self.list_count:
                raise ValueError(
                    "L'indice della lista associata alla preferenza non è valido."
                )


@dataclass(frozen=True)
class EncryptedBallot:
    """
    Rappresenta una scheda politica cifrata.

    Contiene soltanto i ciphertext prodotti dal votante.
    """

    list_ciphertexts: tuple[Ciphertext, ...]
    blank_ciphertext: Ciphertext
    preference_ciphertexts: tuple[Ciphertext, ...]

    def all_ciphertexts(self) -> tuple[Ciphertext, ...]:
        """
        Restituisce tutti i ciphertext della scheda.

        L'ordine è: liste, scheda bianca, preferenze.
        """

        return (
            self.list_ciphertexts
            + (self.blank_ciphertext,)
            + self.preference_ciphertexts
        )


@dataclass(frozen=True)
class BallotProofs:
    """
    Raccoglie le prove pubbliche di validità di una scheda.

    Contiene:
    - una prova R1 per ogni ciphertext della scheda;
    - una prova R2 per la scelta tra liste e scheda bianca;
    - una prova R3 per ogni preferenza;
    - una prova R4 per il numero totale di preferenze;
    - una prova R5 per la regola di genere.
    """

    r1_proofs: tuple[ValueSetProof, ...]
    r2_proof: ValueSetProof
    r3_proofs: tuple[ValueSetProof, ...]
    r4_proof: ValueSetProof
    r5_proof: ValueSetProof


@dataclass(frozen=True)
class BallotWitness:
    """
    Contiene i dati privati usati per generare le prove della scheda.

    Plaintext e nonce servono soltanto durante la costruzione
    delle prove e non fanno parte della scheda pubblica.
    """

    list_plaintexts: tuple[int, ...]
    blank_plaintext: int
    preference_plaintexts: tuple[int, ...]

    list_nonces: tuple[int, ...]
    blank_nonce: int
    preference_nonces: tuple[int, ...]

    def __post_init__(self) -> None:
        """
        Verifica che ogni plaintext abbia il proprio nonce.
        """

        if len(self.list_plaintexts) != len(self.list_nonces):
            raise ValueError(
                "Ogni lista deve avere il proprio nonce."
            )

        if len(self.preference_plaintexts) != len(
            self.preference_nonces
        ):
            raise ValueError(
                "Ogni preferenza deve avere il proprio nonce."
            )


def _validate_ballot_alignment(
    layout: BallotLayout,
    ballot: EncryptedBallot,
    witness: BallotWitness,
) -> None:
    """
    Verifica che configurazione, scheda cifrata e witness
    descrivano la stessa struttura.
    """

    if len(ballot.list_ciphertexts) != layout.list_count:
        raise ValueError(
            "Il numero di liste cifrate non coincide con la configurazione."
        )

    if len(witness.list_plaintexts) != layout.list_count:
        raise ValueError(
            "Il numero di liste del witness non coincide con la configurazione."
        )

    preference_count = len(layout.preference_metadata)

    if len(ballot.preference_ciphertexts) != preference_count:
        raise ValueError(
            "Il numero di preferenze cifrate non coincide con la configurazione."
        )

    if len(witness.preference_plaintexts) != preference_count:
        raise ValueError(
            "Il numero di preferenze del witness non coincide con la configurazione."
        )


def _prove_ballot_r1_r2(
    layout: BallotLayout,
    ballot: EncryptedBallot,
    witness: BallotWitness,
    public_key: int,
    params: GroupParameters,
    context: int,
) -> tuple[tuple[ValueSetProof, ...], ValueSetProof]:
    """
    Genera le prove R1 e R2 per una scheda completa.
    """

    _validate_ballot_alignment(
        layout,
        ballot,
        witness,
    )

    all_plaintexts = (
        witness.list_plaintexts
        + (witness.blank_plaintext,)
        + witness.preference_plaintexts
    )

    all_nonces = (
        witness.list_nonces
        + (witness.blank_nonce,)
        + witness.preference_nonces
    )

    r1_proofs = tuple(
        prove_r1(
            ciphertext=ciphertext,
            plaintext=plaintext,
            nonce=nonce,
            public_key=public_key,
            params=params,
            context=context,
        )
        for ciphertext, plaintext, nonce in zip(
            ballot.all_ciphertexts(),
            all_plaintexts,
            all_nonces,
        )
    )

    r2_ciphertexts = (
        ballot.list_ciphertexts
        + (ballot.blank_ciphertext,)
    )

    r2_plaintexts = (
        witness.list_plaintexts
        + (witness.blank_plaintext,)
    )

    r2_nonces = (
        witness.list_nonces
        + (witness.blank_nonce,)
    )

    r2_proof = prove_r2(
        ciphertexts=r2_ciphertexts,
        plaintexts=r2_plaintexts,
        nonces=r2_nonces,
        public_key=public_key,
        params=params,
        context=context,
    )

    return r1_proofs, r2_proof


def _prove_ballot_r3(
    layout: BallotLayout,
    ballot: EncryptedBallot,
    witness: BallotWitness,
    public_key: int,
    params: GroupParameters,
    context: int,
) -> tuple[ValueSetProof, ...]:
    """
    Genera le prove R3 per tutte le preferenze della scheda.
    """

    _validate_ballot_alignment(
        layout,
        ballot,
        witness,
    )

    proofs = []

    for index, metadata in enumerate(
        layout.preference_metadata
    ):
        list_index = metadata.list_index

        proof = prove_r3(
            list_ciphertext=ballot.list_ciphertexts[
                list_index
            ],
            preference_ciphertext=(
                ballot.preference_ciphertexts[index]
            ),
            list_plaintext=witness.list_plaintexts[
                list_index
            ],
            preference_plaintext=(
                witness.preference_plaintexts[index]
            ),
            list_nonce=witness.list_nonces[
                list_index
            ],
            preference_nonce=(
                witness.preference_nonces[index]
            ),
            public_key=public_key,
            params=params,
            context=context,
        )

        proofs.append(proof)

    return tuple(proofs)


def _prove_ballot_r4(
    layout: BallotLayout,
    ballot: EncryptedBallot,
    witness: BallotWitness,
    public_key: int,
    params: GroupParameters,
    context: int,
) -> ValueSetProof:
    """
    Genera la prova R4 per l'intera scheda.
    """

    _validate_ballot_alignment(
        layout,
        ballot,
        witness,
    )

    return prove_r4(
        preference_ciphertexts=ballot.preference_ciphertexts,
        preference_plaintexts=witness.preference_plaintexts,
        preference_nonces=witness.preference_nonces,
        max_preferences=layout.max_preferences,
        public_key=public_key,
        params=params,
        context=context,
    )


def gender_groups(
    layout: BallotLayout,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """
    Divide le caselle di preferenza tra i due generi.

    Il primo gruppo è quello del genere che viene prima in ordine
    alfabetico (per esempio "F" prima di "M"), il secondo quello
    dell'altro genere; se la scheda ha un solo genere il secondo gruppo
    è vuoto. L'ordine è fissato perché R5 usa la differenza tra il primo
    e il secondo gruppo, e il verificatore deve calcolare la stessa.
    """

    genders = sorted(
        {
            metadata.gender
            for metadata in layout.preference_metadata
        }
    )

    if len(genders) > MAX_GENDERS:
        raise ValueError(
            "La regola di genere confronta al massimo due generi."
        )

    first = tuple(
        index
        for index, metadata in enumerate(layout.preference_metadata)
        if genders and metadata.gender == genders[0]
    )

    second = tuple(
        index
        for index, metadata in enumerate(layout.preference_metadata)
        if len(genders) > 1 and metadata.gender == genders[1]
    )

    return first, second


def _prove_ballot_r5(
    layout: BallotLayout,
    ballot: EncryptedBallot,
    witness: BallotWitness,
    public_key: int,
    params: GroupParameters,
    context: int,
) -> ValueSetProof:
    """
    Genera la prova R5 della scheda (regola di genere).
    """

    _validate_ballot_alignment(
        layout,
        ballot,
        witness,
    )

    first, second = gender_groups(layout)

    return prove_r5(
        first_ciphertexts=tuple(
            ballot.preference_ciphertexts[index]
            for index in first
        ),
        first_plaintexts=tuple(
            witness.preference_plaintexts[index]
            for index in first
        ),
        first_nonces=tuple(
            witness.preference_nonces[index]
            for index in first
        ),
        second_ciphertexts=tuple(
            ballot.preference_ciphertexts[index]
            for index in second
        ),
        second_plaintexts=tuple(
            witness.preference_plaintexts[index]
            for index in second
        ),
        second_nonces=tuple(
            witness.preference_nonces[index]
            for index in second
        ),
        public_key=public_key,
        params=params,
        context=context,
    )


def prove_ballot(
    layout: BallotLayout,
    ballot: EncryptedBallot,
    witness: BallotWitness,
    public_key: int,
    params: GroupParameters,
    context: int,
) -> BallotProofs:
    """
    Genera tutte le prove R1-R5 di una scheda politica.
    """

    _validate_ballot_alignment(
        layout,
        ballot,
        witness,
    )

    r1_proofs, r2_proof = _prove_ballot_r1_r2(
        layout=layout,
        ballot=ballot,
        witness=witness,
        public_key=public_key,
        params=params,
        context=context,
    )

    r3_proofs = _prove_ballot_r3(
        layout=layout,
        ballot=ballot,
        witness=witness,
        public_key=public_key,
        params=params,
        context=context,
    )

    r4_proof = _prove_ballot_r4(
        layout=layout,
        ballot=ballot,
        witness=witness,
        public_key=public_key,
        params=params,
        context=context,
    )

    r5_proof = _prove_ballot_r5(
        layout=layout,
        ballot=ballot,
        witness=witness,
        public_key=public_key,
        params=params,
        context=context,
    )

    return BallotProofs(
        r1_proofs=r1_proofs,
        r2_proof=r2_proof,
        r3_proofs=r3_proofs,
        r4_proof=r4_proof,
        r5_proof=r5_proof,
    )


def verify_ballot(
    layout: BallotLayout,
    ballot: EncryptedBallot,
    proofs: BallotProofs,
    public_key: int,
    params: GroupParameters,
    context: int,
) -> bool:
    """
    Verifica tutte le prove R1-R5 di una scheda politica.

    Restituisce False, senza sollevare eccezioni, anche per una scheda
    malformata: i ciphertext vengono controllati prima di calcolare i
    ciphertext derivati, e le regole si fermano alla prima che fallisce.
    """

    if len(ballot.list_ciphertexts) != layout.list_count:
        return False

    if len(ballot.preference_ciphertexts) != len(
        layout.preference_metadata
    ):
        return False

    all_ciphertexts = ballot.all_ciphertexts()

    if len(proofs.r1_proofs) != len(all_ciphertexts):
        return False

    if len(proofs.r3_proofs) != len(
        ballot.preference_ciphertexts
    ):
        return False

    first, second = gender_groups(layout)

    # Appartenenza al sottogruppo: K e i ciphertext della scheda si
    # controllano una volta sola. I ciphertext derivati per R2-R5 sono
    # prodotti e quozienti di questi, quindi stanno nel sottogruppo, e
    # le prove non devono ricontrollarli.
    if not is_subgroup_element(public_key, params):
        return False

    for ciphertext in all_ciphertexts:
        if not is_subgroup_element(ciphertext.alpha, params):
            return False

        if not is_subgroup_element(ciphertext.beta, params):
            return False

    r1_valid = all(
        verify_r1(
            ciphertext=ciphertext,
            proof=proof,
            public_key=public_key,
            params=params,
            context=context,
            check_membership=False,
        )
        for ciphertext, proof in zip(
            all_ciphertexts,
            proofs.r1_proofs,
        )
    )

    if not r1_valid:
        return False

    r2_valid = verify_r2(
        ciphertexts=(
            ballot.list_ciphertexts
            + (ballot.blank_ciphertext,)
        ),
        proof=proofs.r2_proof,
        public_key=public_key,
        params=params,
        context=context,
        check_membership=False,
    )

    if not r2_valid:
        return False

    r3_valid = all(
        verify_r3(
            list_ciphertext=ballot.list_ciphertexts[
                metadata.list_index
            ],
            preference_ciphertext=(
                ballot.preference_ciphertexts[index]
            ),
            proof=proofs.r3_proofs[index],
            public_key=public_key,
            params=params,
            context=context,
            check_membership=False,
        )
        for index, metadata in enumerate(
            layout.preference_metadata
        )
    )

    if not r3_valid:
        return False

    r4_valid = verify_r4(
        preference_ciphertexts=(
            ballot.preference_ciphertexts
        ),
        proof=proofs.r4_proof,
        max_preferences=layout.max_preferences,
        public_key=public_key,
        params=params,
        context=context,
        check_membership=False,
    )

    if not r4_valid:
        return False

    return verify_r5(
        first_ciphertexts=tuple(
            ballot.preference_ciphertexts[index]
            for index in first
        ),
        second_ciphertexts=tuple(
            ballot.preference_ciphertexts[index]
            for index in second
        ),
        proof=proofs.r5_proof,
        public_key=public_key,
        params=params,
        context=context,
        check_membership=False,
    )


def prove_r1(
    ciphertext: Ciphertext,
    plaintext: int,
    nonce: int,
    public_key: int,
    params: GroupParameters,
    context: int,
) -> ValueSetProof:
    """
    Genera la prova R1 per una singola casella.

    R1 richiede che il valore cifrato appartenga a {0, 1}.
    """

    return prove_value_in_set(
        ciphertext=ciphertext,
        plaintext=plaintext,
        nonce=nonce,
        allowed_values=(0, 1),
        public_key=public_key,
        params=params,
        context=context,
    )


def verify_r1(
    ciphertext: Ciphertext,
    proof: ValueSetProof,
    public_key: int,
    params: GroupParameters,
    context: int,
    check_membership: bool = True,
) -> bool:
    """
    Verifica la prova R1 di una singola casella.

    check_membership=False si usa quando K e il ciphertext sono già
    stati controllati (vedi verify_ballot).
    """

    return verify_value_in_set(
        ciphertext=ciphertext,
        proof=proof,
        allowed_values=(0, 1),
        public_key=public_key,
        params=params,
        context=context,
        check_membership=check_membership,
    )


def _multiply_all(
    ciphertexts: tuple[Ciphertext, ...],
    params: GroupParameters,
) -> Ciphertext:
    """
    Moltiplica una sequenza di ciphertext.

    La sequenza vuota dà (1, 1), cioè Enc(0) con nonce 0: serve per
    una circoscrizione in cui le liste hanno solo il capolista e
    quindi non ci sono caselle di preferenza.
    """

    result = Ciphertext(
        alpha=1,
        beta=1,
    )

    for ciphertext in ciphertexts:
        result = multiply_ciphertexts(
            result,
            ciphertext,
            params,
        )

    return result


def prove_r2(
    ciphertexts: tuple[Ciphertext, ...],
    plaintexts: tuple[int, ...],
    nonces: tuple[int, ...],
    public_key: int,
    params: GroupParameters,
    context: int,
) -> ValueSetProof:
    """
    Genera la prova R2.

    I ciphertext rappresentano i bit delle liste e della scheda bianca.
    La loro somma deve essere esattamente 1.
    """

    if not (
        len(ciphertexts)
        == len(plaintexts)
        == len(nonces)
    ):
        raise ValueError(
            "Ciphertext, plaintext e nonce devono avere la stessa lunghezza."
        )

    derived_ciphertext = _multiply_all(
        ciphertexts,
        params,
    )

    total_plaintext = sum(plaintexts)
    derived_nonce = sum(nonces) % params.q

    return prove_value_in_set(
        ciphertext=derived_ciphertext,
        plaintext=total_plaintext,
        nonce=derived_nonce,
        allowed_values=(1,),
        public_key=public_key,
        params=params,
        context=context,
    )


def verify_r2(
    ciphertexts: tuple[Ciphertext, ...],
    proof: ValueSetProof,
    public_key: int,
    params: GroupParameters,
    context: int,
    check_membership: bool = True,
) -> bool:
    """
    Verifica la prova R2.

    Il prodotto omomorfico dei ciphertext deve cifrare esattamente 1.
    """

    derived_ciphertext = _multiply_all(
        ciphertexts,
        params,
    )

    return verify_value_in_set(
        ciphertext=derived_ciphertext,
        proof=proof,
        allowed_values=(1,),
        public_key=public_key,
        params=params,
        context=context,
        check_membership=check_membership,
    )


def prove_r3(
    list_ciphertext: Ciphertext,
    preference_ciphertext: Ciphertext,
    list_plaintext: int,
    preference_plaintext: int,
    list_nonce: int,
    preference_nonce: int,
    public_key: int,
    params: GroupParameters,
    context: int,
) -> ValueSetProof:
    """
    Genera la prova R3 per una singola preferenza.

    La differenza lista - preferenza deve appartenere a {0, 1}.
    """

    derived_ciphertext = divide_ciphertexts(
        list_ciphertext,
        preference_ciphertext,
        params,
    )

    derived_plaintext = list_plaintext - preference_plaintext
    derived_nonce = (list_nonce - preference_nonce) % params.q

    return prove_value_in_set(
        ciphertext=derived_ciphertext,
        plaintext=derived_plaintext,
        nonce=derived_nonce,
        allowed_values=(0, 1),
        public_key=public_key,
        params=params,
        context=context,
    )


def verify_r3(
    list_ciphertext: Ciphertext,
    preference_ciphertext: Ciphertext,
    proof: ValueSetProof,
    public_key: int,
    params: GroupParameters,
    context: int,
    check_membership: bool = True,
) -> bool:
    """
    Verifica la prova R3 per una singola preferenza.
    """

    derived_ciphertext = divide_ciphertexts(
        list_ciphertext,
        preference_ciphertext,
        params,
    )

    return verify_value_in_set(
        ciphertext=derived_ciphertext,
        proof=proof,
        allowed_values=(0, 1),
        public_key=public_key,
        params=params,
        context=context,
        check_membership=check_membership,
    )


def prove_r4(
    preference_ciphertexts: tuple[Ciphertext, ...],
    preference_plaintexts: tuple[int, ...],
    preference_nonces: tuple[int, ...],
    max_preferences: int,
    public_key: int,
    params: GroupParameters,
    context: int,
) -> ValueSetProof:
    """
    Genera la prova R4.

    La somma complessiva delle preferenze deve appartenere
    all'insieme {0, ..., max_preferences}.
    """

    if not (
        len(preference_ciphertexts)
        == len(preference_plaintexts)
        == len(preference_nonces)
    ):
        raise ValueError(
            "Ciphertext, plaintext e nonce devono avere la stessa lunghezza."
        )

    derived_ciphertext = _multiply_all(
        preference_ciphertexts,
        params,
    )

    total_plaintext = sum(preference_plaintexts)
    derived_nonce = sum(preference_nonces) % params.q

    return prove_value_in_set(
        ciphertext=derived_ciphertext,
        plaintext=total_plaintext,
        nonce=derived_nonce,
        allowed_values=tuple(
            range(max_preferences + 1)
        ),
        public_key=public_key,
        params=params,
        context=context,
    )


def verify_r4(
    preference_ciphertexts: tuple[Ciphertext, ...],
    max_preferences: int,
    proof: ValueSetProof,
    public_key: int,
    params: GroupParameters,
    context: int,
    check_membership: bool = True,
) -> bool:
    """
    Verifica la prova R4.

    La somma delle preferenze deve essere compresa tra 0 e
    max_preferences.
    """

    derived_ciphertext = _multiply_all(
        preference_ciphertexts,
        params,
    )

    return verify_value_in_set(
        ciphertext=derived_ciphertext,
        proof=proof,
        allowed_values=tuple(
            range(max_preferences + 1)
        ),
        public_key=public_key,
        params=params,
        context=context,
        check_membership=check_membership,
    )


def gender_difference_ciphertext(
    first_ciphertexts: tuple[Ciphertext, ...],
    second_ciphertexts: tuple[Ciphertext, ...],
    params: GroupParameters,
) -> Ciphertext:
    """
    Cifrato derivato della regola di genere R5.

    Il prodotto dei cifrati del primo genere diviso il prodotto di
    quelli del secondo è la cifratura della differenza tra le loro
    preferenze; moltiplicandolo per (1, g), cioè per la cifratura di 1
    con nonce 0, si ottiene

        Enc(preferenze del primo genere - preferenze del secondo + 1).

    Il +1 serve a non avere valori negativi: la regola chiede che la
    differenza valga -1, 0 oppure 1, quindi il cifrato derivato deve
    contenere 0, 1 oppure 2.
    """

    difference = divide_ciphertexts(
        _multiply_all(first_ciphertexts, params),
        _multiply_all(second_ciphertexts, params),
        params,
    )

    return Ciphertext(
        alpha=difference.alpha,
        beta=difference.beta * params.g % params.p,
    )


def prove_r5(
    first_ciphertexts: tuple[Ciphertext, ...],
    first_plaintexts: tuple[int, ...],
    first_nonces: tuple[int, ...],
    second_ciphertexts: tuple[Ciphertext, ...],
    second_plaintexts: tuple[int, ...],
    second_nonces: tuple[int, ...],
    public_key: int,
    params: GroupParameters,
    context: int,
) -> ValueSetProof:
    """
    Genera la prova R5, la regola di genere.

    Le preferenze dei due generi devono differire al più di 1: il
    cifrato derivato di gender_difference_ciphertext deve contenere
    0, 1 oppure 2. Con al massimo tre preferenze equivale a dire che
    più preferenze riguardano candidati di genere diverso, come chiede
    la legge. Una scheda che viola la regola non ottiene la prova.
    """

    if not (
        len(first_ciphertexts)
        == len(first_plaintexts)
        == len(first_nonces)
    ) or not (
        len(second_ciphertexts)
        == len(second_plaintexts)
        == len(second_nonces)
    ):
        raise ValueError(
            "Ciphertext, plaintext e nonce devono avere la stessa lunghezza."
        )

    derived_ciphertext = gender_difference_ciphertext(
        first_ciphertexts,
        second_ciphertexts,
        params,
    )

    derived_plaintext = sum(first_plaintexts) - sum(second_plaintexts) + 1

    # La cifratura di 1 aggiunta ha nonce 0: il nonce del cifrato
    # derivato è la differenza delle somme dei nonce.
    derived_nonce = (sum(first_nonces) - sum(second_nonces)) % params.q

    return prove_value_in_set(
        ciphertext=derived_ciphertext,
        plaintext=derived_plaintext,
        nonce=derived_nonce,
        allowed_values=R5_ALLOWED_VALUES,
        public_key=public_key,
        params=params,
        context=context,
    )


def verify_r5(
    first_ciphertexts: tuple[Ciphertext, ...],
    second_ciphertexts: tuple[Ciphertext, ...],
    proof: ValueSetProof,
    public_key: int,
    params: GroupParameters,
    context: int,
    check_membership: bool = True,
) -> bool:
    """
    Verifica la prova R5, la regola di genere.
    """

    derived_ciphertext = gender_difference_ciphertext(
        first_ciphertexts,
        second_ciphertexts,
        params,
    )

    return verify_value_in_set(
        ciphertext=derived_ciphertext,
        proof=proof,
        allowed_values=R5_ALLOWED_VALUES,
        public_key=public_key,
        params=params,
        context=context,
        check_membership=check_membership,
    )