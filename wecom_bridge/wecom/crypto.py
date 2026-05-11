import base64
import hashlib
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class WeComCrypto:
    def __init__(self, token: str, encoding_aes_key: str, corp_id: str) -> None:
        if len(encoding_aes_key) != 43:
            raise SystemExit("WECOM_ENCODING_AES_KEY must be 43 characters")
        self.token = token
        self.corp_id = corp_id
        self.aes_key = base64.b64decode(encoding_aes_key + "=")
        if len(self.aes_key) != 32:
            raise SystemExit("Invalid WECOM_ENCODING_AES_KEY")
        self.iv = self.aes_key[:16]

    def verify_signature(self, signature: str, timestamp: str, nonce: str, encrypted: str) -> None:
        items = [self.token, timestamp, nonce, encrypted]
        expected = hashlib.sha1("".join(sorted(items)).encode("utf-8")).hexdigest()
        if expected != signature:
            raise ValueError("invalid msg_signature")

    def decrypt(self, encrypted: str) -> str:
        ciphertext = base64.b64decode(encrypted)
        decryptor = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        plaintext = self._pkcs7_unpad(padded)
        if len(plaintext) < 20:
            raise ValueError("invalid plaintext")
        msg_len = struct.unpack(">I", plaintext[16:20])[0]
        msg = plaintext[20 : 20 + msg_len]
        corp_id = plaintext[20 + msg_len :].decode("utf-8")
        if corp_id != self.corp_id:
            raise ValueError("corp id mismatch")
        return msg.decode("utf-8")

    @staticmethod
    def _pkcs7_unpad(data: bytes) -> bytes:
        if not data:
            raise ValueError("empty padded data")
        pad = data[-1]
        if pad < 1 or pad > 32:
            raise ValueError("invalid padding")
        if data[-pad:] != bytes([pad]) * pad:
            raise ValueError("invalid padding bytes")
        return data[:-pad]
