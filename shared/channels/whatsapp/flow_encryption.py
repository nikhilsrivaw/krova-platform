"""
Encrypt/decrypt for a WhatsApp Flow's data_exchange endpoint.

Confirmed against Meta's own Business Encryption API and Flow-endpoint
implementation docs (fetched live, not from memory) - this is genuinely
different machinery from shared/auth/encryption.py's Fernet (which
protects our own stored credentials at rest). This is a hybrid RSA/AES
scheme Meta itself drives, one RSA keypair per WhatsApp phone number:

  Every data_exchange request arrives as three base64 fields -
  encrypted_flow_data, encrypted_aes_key, initial_vector. The AES key is
  RSA-OAEP(SHA-256) encrypted with our public key; decrypt it with our
  private key, then AES-256-GCM-decrypt the payload with that key and the
  IV (the 128-bit GCM tag is appended to the ciphertext, not separate).

  The response goes back AES-GCM-encrypted with the SAME key but the IV
  flipped bit-for-bit (every byte XORed with 0xFF) - Meta's own
  requirement, easy to miss and silently produces an undecryptable
  response on their side.

The private key is stored encrypted at rest via shared/auth/encryption.py
(the existing Fernet mechanism), inside ChannelConnection.extra - same
"small credential" pattern as the access token itself, not a new table.
"""

import base64
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class FlowDecryptionError(Exception):
    """The request could not be decrypted - wrong key, tampered payload, or malformed input."""


def generate_keypair() -> tuple[str, str]:
    """(public_pem, private_pem) - a fresh 2048-bit RSA keypair, PEM-encoded as Meta's upload API requires."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return public_pem, private_pem


def decrypt_request(
    private_key_pem: str, encrypted_flow_data_b64: str, encrypted_aes_key_b64: str, initial_vector_b64: str,
) -> tuple[dict, bytes, bytes]:
    """
    Returns (decrypted JSON body, the raw AES key, the raw IV) - the caller
    needs the key and IV again to encrypt the response, so they're handed
    back rather than re-derived.
    """
    try:
        private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
        aes_key = private_key.decrypt(
            base64.b64decode(encrypted_aes_key_b64),
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        iv = base64.b64decode(initial_vector_b64)
        ciphertext_and_tag = base64.b64decode(encrypted_flow_data_b64)
        plaintext = AESGCM(aes_key).decrypt(iv, ciphertext_and_tag, None)
    except Exception as exc:
        raise FlowDecryptionError(str(exc)) from exc

    try:
        body = json.loads(plaintext)
    except ValueError as exc:
        raise FlowDecryptionError("decrypted payload was not valid JSON") from exc
    return body, aes_key, iv


def encrypt_response(response: dict, aes_key: bytes, iv: bytes) -> str:
    """Base64 ciphertext+tag, AES-GCM with the SAME key but the IV flipped byte-for-byte - Meta's own requirement."""
    flipped_iv = bytes(b ^ 0xFF for b in iv)
    plaintext = json.dumps(response).encode()
    ciphertext_and_tag = AESGCM(aes_key).encrypt(flipped_iv, plaintext, None)
    return base64.b64encode(ciphertext_and_tag).decode()
