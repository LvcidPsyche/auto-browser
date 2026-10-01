#!/usr/bin/env python3
"""Independently verify an Auto Browser witness bundle.

This script deliberately imports NOTHING from auto-browser. That is the whole
point: a receipt bundle is only evidence if someone who does not run — and does
not trust — the system that produced it can check it. Its only dependency is
`cryptography`, and that is needed solely for Ed25519 verification; the hash
chain is checked with the standard library alone.

    python verify_witness_bundle.py bundle.json [--expect-key-id ID] [--expect-head HASH]
                                                [--allow-unsigned]

Exit codes:
    0  chain and signatures verified
    1  verification failed
    2  usage or input error

What a PASS means, precisely:

  * every receipt's `chain_hash` matches its content, so nothing was edited
  * every `chain_prev_hash` matches its predecessor, so nothing was reordered,
    inserted, or removed from the middle
  * every receipt from the first signed one on is signed, and every signature
    verifies against the bundled public key; receipts before the first signed
    one (written before signing existed) are covered by its hash
  * the key id printed below is computed from that public key, and the head
    printed below is the hash the receipts actually end at — neither is copied
    from what the bundle says about itself

What a PASS does NOT mean:

  * that the key belongs to who you think. Compare the printed key id with one
    obtained out of band, the way you would an SSH fingerprint, or pass it with
    --expect-key-id.
  * that the tail is complete. Dropping the last receipts leaves a shorter chain
    whose signatures still verify. Compare the printed head with a head obtained
    independently, or pass it with --expect-head.

A bundle with no signed receipt at all fails: its hashes are consistent, but
anyone holding the receipts can produce that. --allow-unsigned accepts it
for deployments that never signed, and says so in the result.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sys
from typing import Any

CHAIN_EXCLUDED_FIELDS = ("chain_hash", "chain_signature", "signing_key_id")


def canonical_chain_hash(receipt: dict[str, Any]) -> str:
    """Recompute a receipt's chain hash exactly as the recorder does."""
    payload = {k: v for k, v in receipt.items() if k not in CHAIN_EXCLUDED_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    prev = receipt.get("chain_prev_hash") or ""
    return hashlib.sha256(f"{prev}:{canonical}".encode("utf-8")).hexdigest()


def key_id_for(public_key_b64: str) -> str | None:
    """SHA-256 of the raw public key: the id the recorder signs under."""
    try:
        return hashlib.sha256(base64.b64decode(public_key_b64, validate=True)).hexdigest()
    except (binascii.Error, ValueError):
        return None


def verify_signature(public_key_b64: str, chain_hash: str, signature_b64: str) -> bool:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), chain_hash.encode("utf-8"))
        return True
    except Exception:
        return False


def _check_key(bundle: dict[str, Any], expect_key_id: str | None, failures: list[str]) -> tuple[str | None, str | None]:
    public_key = bundle.get("public_key_b64")
    key_id = key_id_for(public_key) if public_key else None
    if public_key and key_id is None:
        failures.append("the bundled public key is not valid base64")
    claimed = bundle.get("signing_key_id")
    if key_id and claimed and claimed != key_id:
        failures.append(f"bundle names signing key {claimed} but carries the public key of {key_id}")
    if expect_key_id and key_id != expect_key_id:
        failures.append(f"signed by key {key_id}, not the expected {expect_key_id}")
    return public_key, key_id


def _check_chain(receipts: list[dict[str, Any]], public_key: str | None, key_id: str | None, failures: list[str]) -> tuple[int, int]:
    previous: str | None = None
    signed = unsigned = 0
    for index, receipt in enumerate(receipts):
        label = f"receipt {index} ({receipt.get('receipt_id')})"
        if canonical_chain_hash(receipt) != receipt.get("chain_hash"):
            failures.append(f"{label}: content altered after write")
            break
        if receipt.get("chain_prev_hash") != previous:
            failures.append(f"{label}: chain broken — reordered, truncated from the middle, or forked")
            break
        signature = receipt.get("chain_signature")
        if not signature:
            if signed:
                failures.append(f"{label}: unsigned after signed receipts — a signature was removed")
                break
            unsigned += 1
        elif not public_key:
            failures.append(f"{label}: signed, but the bundle carries no public key")
            break
        elif receipt.get("signing_key_id") and receipt.get("signing_key_id") != key_id:
            failures.append(f"{label}: signed under key {receipt.get('signing_key_id')}, not the bundled key {key_id}")
            break
        elif not verify_signature(public_key, receipt["chain_hash"], signature):
            failures.append(f"{label}: signature does not verify")
            break
        else:
            signed += 1
        previous = receipt.get("chain_hash")
    return signed, unsigned


def verify_bundle(
    bundle: dict[str, Any],
    *,
    allow_unsigned: bool = False,
    expect_key_id: str | None = None,
    expect_head: str | None = None,
) -> dict[str, Any]:
    """Check a bundle. Returns ok, failures, notes, and what was computed from it."""
    failures: list[str] = []
    notes: list[str] = []
    receipts = bundle.get("receipts") or []
    public_key, key_id = _check_key(bundle, expect_key_id, failures)
    signed, unsigned = _check_chain(receipts, public_key, key_id, failures)

    head = receipts[-1].get("chain_hash") if receipts else None
    if bundle.get("head_hash") != head:
        failures.append(f"bundle claims head {bundle.get('head_hash')} but its receipts end at {head} — receipts missing")
    if "receipt_count" in bundle and bundle["receipt_count"] != len(receipts):
        failures.append(f"bundle claims {bundle['receipt_count']} receipts but carries {len(receipts)}")
    if expect_head and head != expect_head:
        failures.append(f"the chain ends at {head}, not the expected head {expect_head}")

    if receipts and not signed:
        message = "no receipt is signed: the hashes are consistent, which anyone holding the receipts can produce"
        (notes if allow_unsigned else failures).append(message)
    elif unsigned:
        notes.append(f"{unsigned} unsigned receipt(s) precede the first signed one, whose signature covers them")
    if not receipts:
        notes.append("bundle contains no receipts")

    return {
        "ok": not failures,
        "failures": failures,
        "notes": notes,
        "key_id": key_id,
        "head_hash": head,
        "receipts": len(receipts),
        "signed": signed,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Verify an Auto Browser witness bundle.", epilog=__doc__)
    parser.add_argument("bundle")
    parser.add_argument("--expect-key-id", help="fail unless the bundle is signed by this key id")
    parser.add_argument("--expect-head", help="fail unless the chain ends at this head hash")
    parser.add_argument("--allow-unsigned", action="store_true", help="accept a bundle with no signed receipt")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        return 2
    try:
        with open(args.bundle, encoding="utf-8") as handle:
            bundle = json.load(handle)
    except Exception as exc:
        print(f"could not read bundle: {exc}", file=sys.stderr)
        return 2

    if not isinstance(bundle, dict) or bundle.get("format") != "auto-browser-witness-bundle":
        print(f"not a witness bundle: format={bundle.get('format') if isinstance(bundle, dict) else None!r}", file=sys.stderr)
        return 2

    result = verify_bundle(
        bundle,
        allow_unsigned=args.allow_unsigned,
        expect_key_id=args.expect_key_id,
        expect_head=args.expect_head,
    )

    print(f"scope           : {bundle.get('scope')}")
    print(f"receipts        : {result['receipts']} ({result['signed']} signed)")
    print(f"head_hash       : {result['head_hash']}   (computed from the receipts)")
    print(f"signing_key_id  : {result['key_id']}   (computed from the public key)")
    print(f"algorithm       : {bundle.get('algorithm')}")
    for failure in result["failures"]:
        print(f"  FAIL: {failure}")
    for note in result["notes"]:
        print(f"  note: {note}")
    print()
    if not result["ok"]:
        print("RESULT: FAILED")
        return 1
    if not result["signed"] and result["receipts"]:
        print("RESULT: VERIFIED (UNSIGNED — consistent hashes only, not evidence of who wrote them)")
        return 0
    print("RESULT: VERIFIED")
    print("Compare signing_key_id and head_hash with values obtained independently, or pass --expect-key-id/--expect-head.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
