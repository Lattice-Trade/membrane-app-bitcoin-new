import pytest

from typing import List, Union

import hmac
from hashlib import sha256
from decimal import Decimal

from bitcoin_client.ledger_bitcoin import Client
from bitcoin_client.ledger_bitcoin.client_base import TransportClient
from bitcoin_client.ledger_bitcoin.exception.errors import IncorrectDataError, NotSupportedError
from bitcoin_client.ledger_bitcoin.psbt import PSBT
from bitcoin_client.ledger_bitcoin.wallet import WalletPolicy

from test_utils import SpeculosGlobals, get_internal_xpub, count_internal_keys

from speculos.client import SpeculosClient
from test_utils.speculos import automation

from .conftest import create_new_wallet, generate_blocks, get_unique_wallet_name, get_wallet_rpc, testnet_to_regtest_addr as T
from .conftest import AuthServiceProxy


def run_test_e2e(wallet_policy: WalletPolicy, core_wallet_names: List[str], rpc: AuthServiceProxy, rpc_test_wallet: AuthServiceProxy, client: Client, speculos_globals: SpeculosGlobals, comm: Union[TransportClient, SpeculosClient]):
    with automation(comm, "automations/register_wallet_accept.json"):
        wallet_id, wallet_hmac = client.register_wallet(wallet_policy)

    assert wallet_id == wallet_policy.id

    assert hmac.compare_digest(
        hmac.new(speculos_globals.wallet_registration_key, wallet_id, sha256).digest(),
        wallet_hmac,
    )

    address_hww = client.get_wallet_address(wallet_policy, wallet_hmac, 0, 3, False)

    # ==> verify the address matches what bitcoin-core computes
    receive_descriptor = wallet_policy.get_descriptor(change=False)
    receive_descriptor_info = rpc.getdescriptorinfo(receive_descriptor)
    # bitcoin-core adds the checksum, and requires it for other calls
    receive_descriptor_chk = receive_descriptor_info["descriptor"]
    address_core = rpc.deriveaddresses(receive_descriptor_chk, [3, 3])[0]

    assert T(address_hww) == address_core

    # also get the change descriptor for later
    change_descriptor = wallet_policy.get_descriptor(change=True)
    change_descriptor_info = rpc.getdescriptorinfo(change_descriptor)
    change_descriptor_chk = change_descriptor_info["descriptor"]

    # ==> import wallet in bitcoin-core

    multisig_wallet_name = get_unique_wallet_name()
    rpc.createwallet(
        wallet_name=multisig_wallet_name,
        disable_private_keys=True,
        descriptors=True,
    )
    multisig_rpc = get_wallet_rpc(multisig_wallet_name)
    multisig_rpc.importdescriptors([{
        "desc": receive_descriptor_chk,
        "active": True,
        "internal": False,
        "timestamp": "now"
    }, {
        "desc": change_descriptor_chk,
        "active": True,
        "internal": True,
        "timestamp": "now"
    }])

    # ==> fund the multisig wallet and get prevout info

    rpc_test_wallet.sendtoaddress(T(address_hww), "0.1")
    generate_blocks(1)

    assert multisig_rpc.getwalletinfo()["balance"] == Decimal("0.1")

    # ==> prepare a psbt spending from the wallet

    out_address = rpc_test_wallet.getnewaddress()

    result = multisig_rpc.walletcreatefundedpsbt(
        outputs={
            out_address: Decimal("0.01")
        },
        options={
            # make sure that the fee is large enough; it looks like
            # fee estimation doesn't work in core with miniscript, yet
            "fee_rate": 10
        })

    psbt_b64 = result["psbt"]

    # ==> sign it with the hww

    psbt = PSBT()
    psbt.deserialize(psbt_b64)

    with automation(comm, "automations/sign_with_wallet_accept.json"):
        hww_sigs = client.sign_psbt(psbt, wallet_policy, wallet_hmac)

    # only correct for taproot policies
    for i, pubkey_augm, sig in hww_sigs:
        if len(pubkey_augm) > 33:
            # signature for a script spend
            assert len(pubkey_augm) == 32 + 32
            pubkey = pubkey_augm[0:32]
            leaf_hash = pubkey_augm[32:64]
            psbt.inputs[i].tap_script_sigs[(pubkey, leaf_hash)] = sig
        else:
            # key path spend
            assert len(pubkey_augm) == 32

            psbt.inputs[i].tap_key_sig = sig

    signed_psbt_hww_b64 = psbt.serialize()

    n_internal_keys = count_internal_keys(speculos_globals.seed, "test", wallet_policy)
    assert len(hww_sigs) == n_internal_keys * len(psbt.inputs)  # should be true as long as all inputs are internal

    # ==> sign it with bitcoin-core

    partial_psbts = [signed_psbt_hww_b64]
    for core_wallet_name in core_wallet_names:
        partial_psbt_response = get_wallet_rpc(core_wallet_name).walletprocesspsbt(psbt_b64)
        partial_psbts.append(partial_psbt_response["psbt"])

    # ==> finalize the psbt, extract tx and broadcast
    combined_psbt = rpc.combinepsbt(partial_psbts)
    result = rpc.finalizepsbt(combined_psbt)

    assert result["complete"] == True
    rawtx = result["hex"]

    # make sure the transaction is valid by broadcasting it (would fail if rejected)
    rpc.sendrawtransaction(rawtx)


def run_test_invalid(client: Client, descriptor_template: str, keys_info: List[str]):
    wallet_policy = WalletPolicy(
        name="Invalid wallet",
        descriptor_template=descriptor_template,
        keys_info=keys_info)

    with pytest.raises((IncorrectDataError, NotSupportedError)):
        client.register_wallet(wallet_policy)


def test_e2e_tapscript_one_of_two_keypath(rpc, rpc_test_wallet, client: Client, speculos_globals: SpeculosGlobals, comm: Union[TransportClient, SpeculosClient]):
    # One of two keys, with the foreign key in the key path spend
    # tr(my_key,pk(foreign_key_1))

    path = "499'/1'/0'"
    _, core_xpub_orig = create_new_wallet()
    internal_xpub = get_internal_xpub(speculos_globals.seed, path)
    wallet_policy = WalletPolicy(
        name="Tapscript 1-of-2",
        descriptor_template="tr(@0/**,pk(@1/**))",
        keys_info=[
            f"[{speculos_globals.master_key_fingerprint.hex()}/{path}]{internal_xpub}",
            f"{core_xpub_orig}",
        ])

    run_test_e2e(wallet_policy, [], rpc, rpc_test_wallet, client, speculos_globals, comm)


def test_e2e_tapscript_one_of_two_scriptpath(rpc, rpc_test_wallet, client: Client, speculos_globals: SpeculosGlobals, comm: Union[TransportClient, SpeculosClient]):
    # One of two keys, with the foreign key in the key path spend
    # tr(foreign_key,pk(my_key))

    path = "499'/1'/0'"
    _, core_xpub_orig = create_new_wallet()
    internal_xpub = get_internal_xpub(speculos_globals.seed, path)
    wallet_policy = WalletPolicy(
        name="Tapscript 1-of-2",
        descriptor_template="tr(@0/**,pk(@1/**))",
        keys_info=[
            f"{core_xpub_orig}",
            f"[{speculos_globals.master_key_fingerprint.hex()}/{path}]{internal_xpub}",
        ])

    run_test_e2e(wallet_policy, [], rpc, rpc_test_wallet, client, speculos_globals, comm)


def test_e2e_tapscript_one_of_three_keypath(rpc, rpc_test_wallet, client: Client, speculos_globals: SpeculosGlobals, comm: Union[TransportClient, SpeculosClient]):
    # One of three keys, with the internal one in the key-path spend
    # tr(my_key,{pk(foreign_key_1,foreign_key_2)})

    path = "499'/1'/0'"
    _, core_xpub_orig_1 = create_new_wallet()
    _, core_xpub_orig_2 = create_new_wallet()
    internal_xpub = get_internal_xpub(speculos_globals.seed, path)
    wallet_policy = WalletPolicy(
        name="Tapscript 1-of-3",
        descriptor_template="tr(@0/**,{pk(@1/**),pk(@2/**)})",
        keys_info=[
            f"[{speculos_globals.master_key_fingerprint.hex()}/{path}]{internal_xpub}",
            f"{core_xpub_orig_1}",
            f"{core_xpub_orig_2}",
        ])

    run_test_e2e(wallet_policy, [],
                 rpc, rpc_test_wallet, client, speculos_globals, comm)


def test_e2e_tapscript_one_of_three_scriptpath(rpc, rpc_test_wallet, client: Client, speculos_globals: SpeculosGlobals, comm: Union[TransportClient, SpeculosClient]):
    # One of three keys, with the internal one in on of the scripts
    # tr(foreign_key_1,{pk(my_key,foreign_key_2)})

    path = "499'/1'/0'"
    _, core_xpub_orig_1 = create_new_wallet()
    _, core_xpub_orig_2 = create_new_wallet()
    internal_xpub = get_internal_xpub(speculos_globals.seed, path)
    wallet_policy = WalletPolicy(
        name="Tapscript 1-of-3",
        descriptor_template="tr(@0/**,{pk(@1/**),pk(@2/**)})",
        keys_info=[
            f"{core_xpub_orig_1}",
            f"[{speculos_globals.master_key_fingerprint.hex()}/{path}]{internal_xpub}",
            f"{core_xpub_orig_2}",
        ])

    run_test_e2e(wallet_policy, [],
                 rpc, rpc_test_wallet, client, speculos_globals, comm)


def test_e2e_tapscript_sortedmulti_a_2of2(rpc, rpc_test_wallet, client: Client, speculos_globals: SpeculosGlobals, comm: Union[TransportClient, SpeculosClient]):
    # tr(foreign_key_1,sortedmulti_a(2,my_key,foreign_key_2))

    path = "499'/1'/0'"
    _, core_xpub_orig_1 = create_new_wallet()
    core_wallet_name2, core_xpub_orig_2 = create_new_wallet()
    internal_xpub = get_internal_xpub(speculos_globals.seed, path)
    wallet_policy = WalletPolicy(
        name="Tapscript 1 or 2-of-2",
        descriptor_template="tr(@0/**,multi_a(2,@1/**,@2/**))",
        keys_info=[
            f"{core_xpub_orig_1}",
            f"[{speculos_globals.master_key_fingerprint.hex()}/{path}]{internal_xpub}",
            f"{core_xpub_orig_2}",
        ])

    run_test_e2e(wallet_policy, [core_wallet_name2],
                 rpc, rpc_test_wallet, client, speculos_globals, comm)
