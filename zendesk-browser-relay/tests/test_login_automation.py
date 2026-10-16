import base64

from zendesk_browser_relay.login_automation import generate_totp_code, mask_secret


def test_generate_totp_code_matches_rfc6238_sha1_vector():
    secret = base64.b32encode(b"12345678901234567890").decode()

    assert generate_totp_code(secret, for_time=59, digits=8) == "94287082"


def test_mask_secret_does_not_expose_full_value():
    assert mask_secret("ABCDEFGHIJKL") == "ABCD...IJKL"
    assert mask_secret("ABC") == "***"
