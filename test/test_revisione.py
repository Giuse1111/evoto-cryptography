"""
Test della revisione del progetto.

Ogni test riproduce un problema trovato durante la revisione e
controlla la correzione:

- una circoscrizione in cui le liste hanno solo il capolista;
- verify_ballot su schede malformate;
- impegni delle prove OR fuori da [1, p - 1];
- controlli di appartenenza al sottogruppo fatti una volta sola;
- verificatore indipendente con registri malformati o costruiti ad arte.
"""

import copy
import json
from pathlib import Path

import pytest

from esperimenti.e5_costi import (
    count_exponentiations,
    synthetic_layout,
)
from evoto.configurazione import (
    build_ballot_layout,
    load_election_config,
)
from evoto.elgamal import Ciphertext
from evoto.garanti import run_key_ceremony
from evoto.gruppo import TEST_PARAMS
from evoto.prove import (
    ValueSetBranchProof,
    ValueSetProof,
    prove_value_in_set,
    verify_value_in_set,
)
from evoto.registro import (
    _ballot_proofs_to_data,
    _ballot_witness_to_data,
    _encrypted_ballot_to_data,
    public_registry_to_json,
)
from evoto.scheda import (
    BallotLayout,
    EncryptedBallot,
    verify_ballot,
)
from evoto.simulazione import simulate_election
from evoto.voto import (
    VoterChoice,
    prepare_ballot,
)
from verifica.verifica import (
    compute_ballot_hash_from_data,
    hash_to_q,
    verify_public_registry,
    verify_r4_rule,
)


CONFIG = load_election_config(
    Path(__file__).resolve().parent.parent
    / "config"
    / "elezione_esempio.json"
)

CEREMONY = run_key_ceremony(
    guardian_count=3,
    quorum=2,
    params=TEST_PARAMS,
    election_id=1,
)

PUBLIC_KEY = CEREMONY.joint_public_key
CONTEXT = CEREMONY.extended_base_hash

REPORT = simulate_election(
    config=CONFIG,
    voters_per_district=3,
    guardian_count=5,
    quorum=3,
    present_guardians=(1, 3, 5),
    params=TEST_PARAMS,
    seed=1,
)

REGISTRY = public_registry_to_json(
    REPORT,
    TEST_PARAMS,
)


def test_district_without_preference_boxes():
    """
    Due liste con il solo capolista: nessuna casella di preferenza.
    R4 lavora sul prodotto vuoto, cioè Enc(0) = (1, 1) con nonce 0,
    e la scheda si cifra e si verifica normalmente.

    Anche il verificatore indipendente accetta la prova R4 sul
    prodotto vuoto.
    """

    layout = BallotLayout(
        list_count=2,
        preference_metadata=(),
        max_preferences=3,
    )

    prepared = prepare_ballot(
        layout,
        0,
        VoterChoice(list_index=1),
        PUBLIC_KEY,
        TEST_PARAMS,
        CONTEXT,
    )

    assert verify_ballot(
        layout,
        prepared.ballot,
        prepared.proofs,
        PUBLIC_KEY,
        TEST_PARAMS,
        CONTEXT,
    )

    branches = tuple(
        (
            branch.commitment_1,
            branch.commitment_2,
            branch.challenge,
            branch.response,
        )
        for branch in prepared.proofs.r4_proof.branches
    )

    assert verify_r4_rule(
        preference_ciphertexts=(),
        branches=branches,
        p=TEST_PARAMS.p,
        q=TEST_PARAMS.q,
        g=TEST_PARAMS.g,
        public_key=PUBLIC_KEY,
        context=CONTEXT,
        max_preferences=3,
    )


def test_verify_ballot_rejects_malformed_ciphertexts_without_raising():
    """
    Una casella con alpha = 0 non appartiene al gruppo: la scheda è
    rifiutata con False, senza arrivare all'inverso di 0 in R3.
    """

    layout = build_ballot_layout(CONFIG, 0)

    prepared = prepare_ballot(
        layout,
        0,
        VoterChoice(list_index=1, preferences=(4,)),
        PUBLIC_KEY,
        TEST_PARAMS,
        CONTEXT,
    )

    preferences = list(prepared.ballot.preference_ciphertexts)
    preferences[0] = Ciphertext(alpha=0, beta=1)

    broken = EncryptedBallot(
        list_ciphertexts=prepared.ballot.list_ciphertexts,
        blank_ciphertext=prepared.ballot.blank_ciphertext,
        preference_ciphertexts=tuple(preferences),
    )

    assert not verify_ballot(
        layout,
        broken,
        prepared.proofs,
        PUBLIC_KEY,
        TEST_PARAMS,
        CONTEXT,
    )


def test_or_proof_commitments_must_be_in_range():
    """
    Un impegno sostituito con a + p rappresenta lo stesso elemento
    modulo p ma non è nella forma canonica [1, p - 1]: la prova è
    rifiutata. Anche valori ammessi negativi vengono rifiutati.
    """

    ciphertext = Ciphertext(alpha=850, beta=2375)

    proof = prove_value_in_set(
        ciphertext=ciphertext,
        plaintext=1,
        nonce=11,
        allowed_values=(0, 1),
        public_key=530,
        params=TEST_PARAMS,
        context=744,
    )

    assert verify_value_in_set(ciphertext, proof, (0, 1), 530, TEST_PARAMS, 744)

    first = proof.branches[0]

    shifted = ValueSetProof(
        branches=(
            ValueSetBranchProof(
                commitment_1=first.commitment_1 + TEST_PARAMS.p,
                commitment_2=first.commitment_2,
                challenge=first.challenge,
                response=first.response,
            ),
            proof.branches[1],
        )
    )

    assert not verify_value_in_set(ciphertext, shifted, (0, 1), 530, TEST_PARAMS, 744)
    assert not verify_value_in_set(ciphertext, proof, (-1, 1), 530, TEST_PARAMS, 744)


def test_membership_is_checked_once_per_ballot():
    """
    Scheda di riferimento della proposta (10 liste, capolista + 8):
    91 ciphertext, cioè 182 componenti, più la chiave K.

    Prima della revisione i controlli x^q = 1 erano 1231 (impegni,
    ciphertext derivati e K ripetuta in ogni prova); ora sono
    182 + 1 = 183.
    """

    layout = synthetic_layout(10, 8)

    prepared = prepare_ballot(
        layout,
        0,
        VoterChoice(list_index=0, preferences=(0, 1, 2)),
        PUBLIC_KEY,
        TEST_PARAMS,
        CONTEXT,
    )

    with count_exponentiations(subgroup_order=TEST_PARAMS.q) as counter:
        assert verify_ballot(
            layout,
            prepared.ballot,
            prepared.proofs,
            PUBLIC_KEY,
            TEST_PARAMS,
            CONTEXT,
        )

    assert counter.membership_checks == 183


def failed(verdict: dict[str, bool]) -> set[str]:
    """
    Controlli falliti di un esito del verificatore.
    """

    return {
        name
        for name, ok in verdict.items()
        if name != "overall" and not ok
    }


def test_registry_of_the_example_is_verified():
    """
    Punto di partenza dei test successivi: il registro è valido.
    """

    assert verify_public_registry(REGISTRY)["overall"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["group"].update(q=0),
        lambda data: data["group"].update(q=False),
        lambda data: data["configuration"]["rules"].update(max_preferences=10**300),
        lambda data: data["configuration"]["rules"].update(
            max_preferences_per_gender=10**12
        ),
        lambda data: data["bulletin_board"]["entries"][1]["proofs"].update(
            r5_proof=None
        ),
        lambda data: data["bulletin_board"]["entries"][1].update(tracking_code=1.5),
    ],
)
def test_malformed_registry_never_raises(mutation):
    """
    Dati malformati fanno fallire i controlli che li usano, ma il
    verificatore non solleva mai eccezioni.
    """

    data = json.loads(REGISTRY)
    mutation(data)

    verdict = verify_public_registry(json.dumps(data))

    assert verdict["overall"] is False


def test_deeply_nested_json_is_rejected():
    """
    Un JSON annidato oltre il limite di ricorsione è un registro non
    valido, non un errore del verificatore.
    """

    verdict = verify_public_registry("[" * 100000)

    assert not any(verdict.values())


def test_identity_public_key_fails_v1():
    """
    K = 1 vorrebbe dire chiave segreta 0: V1 lo rifiuta.
    """

    data = json.loads(REGISTRY)
    data["K"] = 1

    assert "V1" in failed(verify_public_registry(json.dumps(data)))


def test_v7_requires_valid_shares():
    """
    Con una share alterata fallisce V6, e V7, che combina le share,
    non viene accettato.
    """

    data = json.loads(REGISTRY)
    share = data["district_results"][0]["decryption_shares"][0][0]
    share["partial_decryption"] = (
        share["partial_decryption"] * TEST_PARAMS.g % TEST_PARAMS.p
    )

    assert {"V6", "V7"} <= failed(verify_public_registry(json.dumps(data)))


def append_forged_ballot(state: str) -> str:
    """
    Aggiunge in fondo alla bacheca una scheda con una lista in più di
    quelle della configurazione, con prove valide per quella forma e
    la catena dei codici ricalcolata.
    """

    data = json.loads(REGISTRY)
    layout = build_ballot_layout(CONFIG, 0)

    forged_layout = BallotLayout(
        list_count=layout.list_count + 1,
        preference_metadata=layout.preference_metadata,
        max_preferences=layout.max_preferences,
    )

    context = data["election_context"]["Q_bar"]

    prepared = prepare_ballot(
        forged_layout,
        0,
        VoterChoice(list_index=layout.list_count),
        data["K"],
        TEST_PARAMS,
        context,
    )

    entries = data["bulletin_board"]["entries"]
    sequence = len(entries) + 1
    ballot = _encrypted_ballot_to_data(prepared.ballot)
    ballot_hash = compute_ballot_hash_from_data(ballot, 0, context, TEST_PARAMS.q)

    entry = {
        "sequence": sequence,
        "district_index": 0,
        "state": state,
        "ballot": ballot,
        "proofs": _ballot_proofs_to_data(prepared.proofs),
        "ballot_hash": ballot_hash,
        "tracking_code": hash_to_q(
            TEST_PARAMS.q,
            entries[-1]["tracking_code"],
            sequence,
            1 if state == "CAST" else 2,
            ballot_hash,
        ),
    }

    if state == "SPOILED":
        entry["revealed_witness"] = _ballot_witness_to_data(prepared.witness)

    entries.append(copy.deepcopy(entry))

    return json.dumps(data)


def test_ballot_with_an_extra_list_fails_v3():
    """
    Prima della revisione una scheda SPOILED con una lista in più
    superava tutti i controlli. Ora V3 confronta il numero di liste
    della scheda con la configurazione.
    """

    assert "V3" in failed(verify_public_registry(append_forged_ballot("SPOILED")))
    assert "V3" in failed(verify_public_registry(append_forged_ballot("CAST")))
