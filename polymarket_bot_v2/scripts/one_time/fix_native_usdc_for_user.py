#!/usr/bin/env python3
"""
T892 one-time rescue script

Purpose:
- Move ALL native USDC in a specific deposit wallet to the fixed Bridge intake
  via one gasless WALLET batch call (single transfer call).
- User signs typed data once, then this script submits to relayer.

Safety:
- Hardcoded addresses for one user only.
- Requires explicit --confirm token before submit.
- Self-deletes on successful submit by default.
- Do not reuse for other users.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import requests

# One-time hardcoded targets (user requested)
USER_EOA = "0x3992c458171Fb9dE05D753CD33120c594a90C17b"
DEPOSIT_WALLET = "0x3992c458171Fb9dE05D753CD33120c594a90C17b"
NATIVE_USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
BRIDGE_INTAKE = "0x4E559D79A8BB3b131E2311d8899387Ad8a556e18"

CHAIN_ID = 137
USDC_DECIMALS = 6
RELAYER_API = "https://relayer-v2.polymarket.com"
CONFIRM_TOKEN = "T892-ONE-TIME-CONFIRM"


def _rpc_candidates() -> list[str]:
    out: list[str] = []
    for key in (
        "POLYGON_RPC_URL",
        "POLYGON_RPC_HTTP_URL",
        "POLYGON_RPC",
        "ALCHEMY_POLYGON_RPC_URL",
    ):
        value = str(os.environ.get(key) or "").strip()
        if value and value not in out:
            out.append(value)
    ankr = str(os.environ.get("ANKR_API_KEY") or "").strip()
    if ankr:
        out.append(f"https://rpc.ankr.com/polygon/{ankr}")
    for public in (
        "https://polygon-bor-rpc.publicnode.com",
        "https://polygon.drpc.org",
        "https://1rpc.io/matic",
        "https://polygon-rpc.com",
        "https://polygon.llamarpc.com",
        "https://polygon-mainnet.public.blastapi.io",
        "https://rpc-mainnet.maticvigil.com",
    ):
        if public not in out:
            out.append(public)
    return out


def _rpc_call(method: str, params: list, timeout: int = 12):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_err = ""
    for rpc in _rpc_candidates():
        try:
            resp = requests.post(rpc, json=payload, timeout=timeout)
            if resp.status_code >= 400:
                last_err = f"{rpc} http={resp.status_code}"
                continue
            data = resp.json()
            if "error" in data:
                last_err = f"{rpc} rpc_error={data['error']}"
                continue
            return data.get("result")
        except Exception as exc:  # noqa: BLE001
            last_err = f"{rpc} exc={exc}"
    raise RuntimeError(f"all polygon rpc failed: {last_err}")


def _fetch_erc20_balance_raw(wallet: str, token: str) -> int:
    wallet_hex = wallet.lower().replace("0x", "")
    data = "0x70a08231" + ("0" * 24) + wallet_hex
    result = _rpc_call("eth_call", [{"to": token, "data": data}, "latest"])
    if isinstance(result, str) and result.startswith("0x"):
        return int(result, 16)
    return 0


def _build_transfer_calldata(to_addr: str, amount_wei: int) -> str:
    to_hex = to_addr.lower().replace("0x", "")
    amt_hex = format(int(amount_wei), "064x")
    return "0xa9059cbb" + ("0" * 24) + to_hex + amt_hex


def _build_hmac_signature(secret_b64: str, timestamp: int, method: str, path: str, body: str) -> str:
    msg = f"{timestamp}{method}{path}{body}"
    s = secret_b64.strip()
    s_padded = s + "=" * ((4 - len(s) % 4) % 4)
    try:
        key = base64.urlsafe_b64decode(s_padded)
    except Exception:
        key = base64.b64decode(s_padded)
    digest = hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = base64.b64encode(digest).decode("ascii")
    return sig_b64.replace("+", "-").replace("/", "_")


def _relayer_headers(method: str, path: str, body: str) -> dict[str, str]:
    bk = str(os.environ.get("POLY_BUILDER_API_KEY") or "").strip()
    bs = str(os.environ.get("POLY_BUILDER_SECRET") or "").strip()
    bp = str(os.environ.get("POLY_BUILDER_PASSPHRASE") or "").strip()
    if bk and bs and bp:
        ts = int(time.time())
        sig = _build_hmac_signature(bs, ts, method, path, body)
        return {
            "POLY_BUILDER_API_KEY": bk,
            "POLY_BUILDER_PASSPHRASE": bp,
            "POLY_BUILDER_SIGNATURE": sig,
            "POLY_BUILDER_TIMESTAMP": str(ts),
            "Content-Type": "application/json",
        }

    key = str(os.environ.get("RELAYER_API_KEY") or "").strip()
    addr = str(os.environ.get("RELAYER_API_KEY_ADDRESS") or "").strip()
    if key and addr:
        return {
            "RELAYER_API_KEY": key,
            "RELAYER_API_KEY_ADDRESS": addr,
            "Content-Type": "application/json",
        }

    raise EnvironmentError("Missing relayer credentials (POLY_BUILDER_* or RELAYER_API_KEY*)")


def _resolve_relayer_from(owner_eoa: str) -> str:
    has_hmac = bool(
        str(os.environ.get("POLY_BUILDER_API_KEY") or "").strip()
        and str(os.environ.get("POLY_BUILDER_SECRET") or "").strip()
        and str(os.environ.get("POLY_BUILDER_PASSPHRASE") or "").strip()
    )
    if has_hmac:
        return owner_eoa
    legacy = str(os.environ.get("RELAYER_API_KEY_ADDRESS") or "").strip()
    if not legacy:
        raise EnvironmentError("RELAYER_API_KEY_ADDRESS missing for legacy relayer auth")
    return legacy


def _get_relayer_nonce(address: str, nonce_type: str = "WALLET") -> str:
    resp = requests.get(
        f"{RELAYER_API}/nonce",
        params={"address": address, "type": nonce_type},
        timeout=15,
    )
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:500]}
    if resp.status_code >= 400:
        raise RuntimeError(f"relayer nonce failed status={resp.status_code} body={body}")
    nonce = str((body or {}).get("nonce") or "").strip()
    if not nonce:
        raise RuntimeError(f"relayer nonce empty body={body}")
    return nonce


def _submit_relayer(payload: dict) -> dict:
    if "metadata" not in payload:
        payload = {**payload, "metadata": ""}
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    headers = _relayer_headers("POST", "/submit", body)
    resp = requests.post(
        f"{RELAYER_API}/submit",
        headers=headers,
        data=body.encode("utf-8"),
        timeout=20,
    )
    try:
        result = resp.json()
    except Exception:
        result = {"raw": resp.text[:1000]}
    if resp.status_code >= 400:
        raise RuntimeError(f"relayer submit failed status={resp.status_code} body={result}")
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected relayer response: {result}")
    return result


def _build_typed_data(deposit_wallet: str, nonce: str, deadline: int, calls: list[dict]) -> dict:
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Call": [
                {"name": "target", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "data", "type": "bytes"},
            ],
            "Batch": [
                {"name": "wallet", "type": "address"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
                {"name": "calls", "type": "Call[]"},
            ],
        },
        "primaryType": "Batch",
        "domain": {
            "name": "DepositWallet",
            "version": "1",
            "chainId": CHAIN_ID,
            "verifyingContract": deposit_wallet,
        },
        "message": {
            "wallet": deposit_wallet,
            "nonce": int(nonce),
            "deadline": int(deadline),
            "calls": calls,
        },
    }


def _format_usdc(raw: int) -> str:
    return f"{raw / (10 ** USDC_DECIMALS):.6f}"


def _extract_tx_hash(result: dict) -> str:
    for key in ("txHash", "tx_hash", "transactionHash", "hash"):
        val = str(result.get(key) or "").strip()
        if val:
            return val
    return ""


def _self_delete() -> None:
    path = Path(__file__).resolve()
    try:
        os.remove(path)
        print(f"[T892] self-delete success: {path}")
    except Exception as exc:  # noqa: BLE001
        print(f"[T892] self-delete failed: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="One-time native USDC fix for one user")
    parser.add_argument("--signature", default="", help="EIP-712 Batch signature from user wallet")
    parser.add_argument("--confirm", default="", help=f"Required token before submit: {CONFIRM_TOKEN}")
    parser.add_argument("--deadline-seconds", type=int, default=900)
    parser.add_argument("--keep-file", action="store_true", help="Do not self-delete after successful submit")
    args = parser.parse_args()

    print("[T892] one-time script start")
    print(f"[T892] user_eoa={USER_EOA}")
    print(f"[T892] deposit_wallet={DEPOSIT_WALLET}")
    print(f"[T892] bridge_intake={BRIDGE_INTAKE}")

    native_raw = _fetch_erc20_balance_raw(DEPOSIT_WALLET, NATIVE_USDC)
    print(f"[T892] native_usdc_raw={native_raw}")
    print(f"[T892] native_usdc={_format_usdc(native_raw)}")

    if native_raw <= 0:
        print("[T892] nothing to transfer, exit")
        return 0

    call = {
        "target": NATIVE_USDC,
        "value": "0",
        "data": _build_transfer_calldata(BRIDGE_INTAKE, native_raw),
    }
    calls = [call]

    nonce = _get_relayer_nonce(USER_EOA, "WALLET")
    deadline = int(time.time()) + max(60, int(args.deadline_seconds))
    typed_data = _build_typed_data(DEPOSIT_WALLET, nonce, deadline, calls)

    print(f"[T892] nonce={nonce} deadline={deadline}")
    print("[T892] typed_data_json=")
    print(json.dumps(typed_data, ensure_ascii=False, indent=2))

    sig = str(args.signature or "").strip()
    if not sig:
        print("[T892] no --signature provided; preparation complete")
        print("[T892] sign typed_data once, then rerun with --signature and --confirm")
        return 0

    if args.confirm != CONFIRM_TOKEN:
        print("[T892] blocked: invalid --confirm token")
        return 2

    payload = {
        "type": "WALLET",
        "from": _resolve_relayer_from(USER_EOA),
        "to": DEPOSIT_WALLET,
        "nonce": str(nonce),
        "signature": sig,
        "depositWalletParams": {
            "depositWallet": DEPOSIT_WALLET,
            "deadline": str(deadline),
            "calls": calls,
        },
        "metadata": "T892-one-time-native-usdc-rescue",
    }

    print("[T892] submitting relayer batch...")
    result = _submit_relayer(payload)
    print("[T892] relayer_result=")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    tx_hash = _extract_tx_hash(result)
    if tx_hash:
        print(f"[T892] tx_hash={tx_hash}")

    post_raw = _fetch_erc20_balance_raw(DEPOSIT_WALLET, NATIVE_USDC)
    print(f"[T892] post_native_usdc_raw={post_raw}")
    print(f"[T892] post_native_usdc={_format_usdc(post_raw)}")

    if not args.keep_file:
        _self_delete()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
