from cognita.tokens import generate_token, hash_token, token_matches


def test_generate_token_entropy_and_uniqueness():
    t1, t2 = generate_token(), generate_token()
    assert t1 != t2
    assert len(t1) >= 40  # 32 bytes urlsafe-b64 ≈ 43 chars


def test_hash_is_hex_sha256():
    h = hash_token("abc")
    assert len(h) == 64
    assert h == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_token_matches_roundtrip():
    t = generate_token()
    assert token_matches(t, hash_token(t))
    assert not token_matches(t + "x", hash_token(t))
    assert not token_matches(t, hash_token("different"))
