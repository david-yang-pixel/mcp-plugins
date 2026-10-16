from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time


def generate_totp_code(
    secret: str,
    *,
    for_time: int | None = None,
    period: int = 30,
    digits: int = 6,
) -> str:
    normalized = secret.replace(" ", "").upper()
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    key = base64.b32decode(normalized + padding)
    counter = int((time.time() if for_time is None else for_time) // period)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10**digits)).zfill(digits)


def mask_secret(secret: str) -> str:
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}...{secret[-4:]}"
