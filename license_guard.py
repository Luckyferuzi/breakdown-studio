# SPDX-License-Identifier: GPL-3.0-or-later
# BreakdownStudio 3D Pro v2.0 - Hardware-Bound License Key & Anti-Redistribution Guard

import hashlib
import hmac
import json
import os
import platform
import urllib.parse
import urllib.request
import uuid
import bpy

_GUARD_SECRET = b"TumexStudio_BreakdownStudio_v2_HWID_Lock_2026_#7741"
GUMROAD_PRODUCT_PERMALINK = "breakdownstudio"
MAX_MACHINES_PER_KEY = 2
STORE_BUY_URL = "https://tumexstudio.gumroad.com"

_MASTER_KEYS = {
    "TUMEX-OWNER-2026-PRO",
    "BREAKDOWN-STUDIO-VIP-2026",
}


def get_machine_hwid() -> str:
    """Compute a deterministic 16-character Hardware Machine ID (HWID) unique to this computer."""
    raw = f"{uuid.getnode()}|{platform.node()}|{platform.machine()}|{platform.processor()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16].upper()


def _get_license_file_path() -> str:
    cfg_dir = bpy.utils.user_resource('CONFIG')
    os.makedirs(cfg_dir, exist_ok=True)
    return os.path.join(cfg_dir, "tumex_breakdown_license.json")


def _compute_hwid_signature(license_key: str, hwid: str) -> str:
    msg = f"{license_key.strip().upper()}::{hwid.strip().upper()}::BREAKDOWN_V2".encode("utf-8")
    return hmac.new(_GUARD_SECRET, msg, hashlib.sha256).hexdigest()


def generate_signed_customer_key(seed_tag: str = "PRO1") -> str:
    clean = seed_tag.strip().upper()[:4].ljust(4, "X")
    sig = hmac.new(_GUARD_SECRET, f"BS2::{clean}".encode("utf-8"), hashlib.sha256).hexdigest().upper()
    return f"TUMEX-BS2-{clean}-{sig[:4]}-{sig[4:8]}"


def _verify_signed_tumex_key(key_str: str) -> bool:
    k = key_str.strip().upper()
    if k in _MASTER_KEYS:
        return True
    parts = k.split("-")
    if len(parts) == 5 and parts[0] == "TUMEX" and parts[1] == "BS2":
        seed_part = parts[2]
        sig = hmac.new(_GUARD_SECRET, f"BS2::{seed_part}".encode("utf-8"), hashlib.sha256).hexdigest().upper()
        if parts[3] == sig[:4] and parts[4] == sig[4:8]:
            return True
    return False


def _verify_gumroad_key_online(key_str: str) -> tuple[bool, str]:
    clean_key = key_str.strip()
    if len(clean_key) < 16 or "-" not in clean_key:
        return False, "Invalid license key format."

    url = "https://api.gumroad.com/v2/licenses/verify"
    data = urllib.parse.urlencode({
        "product_permalink": GUMROAD_PRODUCT_PERMALINK,
        "license_key": clean_key,
        "increment_uses_count": "true",
    }).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("success"):
                purchase = payload.get("purchase", {})
                if purchase.get("refunded") or purchase.get("chargebacked"):
                    return False, "This license key has been refunded or cancelled."
                uses = int(payload.get("uses", 1))
                if uses > MAX_MACHINES_PER_KEY:
                    return (
                        False,
                        f"Key limit reached ({uses}/{MAX_MACHINES_PER_KEY} PCs used). Sharing keys is blocked.",
                    )
                return True, f"Verified via Gumroad ({uses}/{MAX_MACHINES_PER_KEY} PC activations used)."
            return False, payload.get("message", "License key not recognized.")
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            return False, err_body.get("message", "Invalid Gumroad license key.")
        except Exception:
            return False, "Invalid license key."
    except Exception:
        return False, "Could not verify key online. Please check your internet connection or key."


def is_machine_licensed() -> bool:
    lic_path = _get_license_file_path()
    if not os.path.exists(lic_path):
        return False

    try:
        with open(lic_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        saved_key = data.get("license_key", "")
        saved_hwid = data.get("hwid", "")
        saved_sig = data.get("signature", "")

        current_hwid = get_machine_hwid()
        if saved_hwid != current_hwid:
            return False

        expected_sig = _compute_hwid_signature(saved_key, current_hwid)
        return hmac.compare_digest(saved_sig, expected_sig)
    except Exception:
        return False


def activate_license_on_this_machine(raw_key: str) -> tuple[bool, str]:
    clean_key = raw_key.strip().upper()
    if not clean_key:
        return False, "Please enter a License Key first."

    if _verify_signed_tumex_key(clean_key):
        ok, msg = True, "License activated and locked to this PC Hardware ID!"
    else:
        ok, msg = _verify_gumroad_key_online(raw_key)

    if not ok:
        return False, msg

    hwid = get_machine_hwid()
    sig = _compute_hwid_signature(clean_key, hwid)
    lic_path = _get_license_file_path()
    with open(lic_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "product": "BreakdownStudio 3D Pro v2.0",
                "license_key": clean_key,
                "hwid": hwid,
                "signature": sig,
            },
            f,
            indent=2,
        )
    return True, msg


def deactivate_license_on_this_machine():
    lic_path = _get_license_file_path()
    if os.path.exists(lic_path):
        try:
            os.remove(lic_path)
        except Exception:
            pass
