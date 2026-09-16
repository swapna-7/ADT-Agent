from pathlib import Path

from self_update import verify_binary_signature


def test_signature_failure_on_garbage(tmp_path: Path):
    binary = tmp_path / "agent.bin"
    binary.write_bytes(b"not-a-signed-binary")
    assert verify_binary_signature(binary, "not-a-signature") is False
