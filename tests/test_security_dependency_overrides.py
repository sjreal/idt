import json

from json_repair import repair_json
from presidio_anonymizer.operators.aes_cipher import AESCipher


def test_json_repair_override_keeps_llm_guard_api_compatible() -> None:
    repaired = repair_json("{'answer': 'safe'}", skip_json_loads=True, return_objects=False)

    assert json.loads(repaired) == {"answer": "safe"}


def test_cryptography_override_keeps_presidio_aes_operator_compatible() -> None:
    key = bytes(range(32))
    encrypted = AESCipher.encrypt(key, "synthetic test value")

    assert AESCipher.decrypt(key, encrypted) == "synthetic test value"
