#!/usr/bin/env python3
"""Independently verify an Auto Browser witness bundle.

This script deliberately imports NOTHING from auto-browser. That is the whole
point: a receipt bundle is only evidence if someone who does not run — and does
not trust — the system that produced it can check it. Its only dependency is
`cryptography`, and that is needed solely for Ed25519 verification; the hash
chain is checked with the standard library alone.

    python verify_witness_bundle.py bundle.json

Exit codes:
    0  chain and signatures verified
    1  verification failed
    2  usage or input error

What a PASS means, precisely:

  * every receipt's `chain_hash` matches its content, so nothing was edited
  * every `chain_prev_hash` matches its predecessor, so nothing was reordered,
    inserted, or removed from the middle
  * every signature verifies against the bundled public key, so the holder of
    the corresponding private key attested to each receipt and its history

What a PASS does NOT mean:

  * that the tail is complete. Dropping the last k receipts leaves a shorter
    chain whose remaining signatures are all still valid. Compare `head_hash`
    against a head you obtained previously and independently.
  * that the key belongs to who you think. Verify `signing_key_id` (the SHA-256
    of the public key) out of band, the same way you would an SSH fingerprint.
"""

from __future__ import annotations

import base64
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


def verify_signature(public_key_b64: str, chain_hash: str, signature_b64: str) -> bool:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), chain_hash.encode("utf-8"))
        return True
    except Exception:
        return False


def verify_bundle(bundle: dict[str, Any]) -> tuple[bool, list[str]]:
    problems: list[str] = []
    receipts = bundle.get("receipts") or []
    public_key = bundle.get("public_key_b64")

    if not receipts:
        return True, ["bundle contains no receipts"]

    previous: str | None = None
    unsigned = 0
    for index, receipt in enumerate(receipts):
        recomputed = canonical_chain_hash(receipt)
        if recomputed != receipt.get("chain_hash"):
            problems.append(f"receipt {index} ({receipt.get('receipt_id')}): content altered after write")
            break
        if receipt.get("chain_prev_hash") != previous:
            problems.append(
                f"receipt {index} ({receipt.get('receipt_id')}): chain broken — "
                "reordered, truncated from the middle, or forked"
            )
            break

        signature = receipt.get("chain_signature")
        if not signature:
            unsigned += 1
        elif not public_key:
            problems.append(f"receipt {index}: signed, but the bundle carries no public key")
            break
        elif not verify_signature(public_key, receipt["chain_hash"], signature):
            problems.append(f"receipt {index} ({receipt.get('receipt_id')}): signature does not verify")
            break

        previous = receipt.get("chain_hash")

    if unsigned:
        problems.append(f"{unsigned} of {len(receipts)} receipts are unsigned (pre-dating signing, or a signer outage)")

    fatal = [p for p in problems if "unsigned" not in p and "no receipts" not in p]
    return not fatal, problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        with open(argv[1], encoding="utf-8") as handle:
            bundle = json.load(handle)
    except Exception as exc:
        print(f"could not read bundle: {exc}", file=sys.stderr)
        return 2

    if bundle.get("format") != "auto-browser-witness-bundle":
        print(f"not a witness bundle: format={bundle.get('format')!r}", file=sys.stderr)
        return 2

    ok, problems = verify_bundle(bundle)

    print(f"scope           : {bundle.get('scope')}")
    print(f"receipts        : {len(bundle.get('receipts') or [])}")
    print(f"head_hash       : {bundle.get('head_hash')}")
    print(f"signing_key_id  : {bundle.get('signing_key_id')}")
    print(f"algorithm       : {bundle.get('algorithm')}")
    for problem in problems:
        print(f"  note: {problem}")
    print()
    print("RESULT: VERIFIED" if ok else "RESULT: FAILED")
    if ok:
        print("Compare head_hash against an independently obtained head to rule out tail truncation.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
