import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models


class DecryptionError(Exception):
    """A stored value could not be decrypted (wrong key or corrupted data)."""


def _get_fernet() -> Fernet:
    """Build the Fernet from FIELD_ENCRYPTION_KEY, or derive it from SECRET_KEY if unset."""
    key = getattr(settings, "FIELD_ENCRYPTION_KEY", "") or ""
    if not key:
        raw = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        key = base64.urlsafe_b64encode(raw)
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key)


def encrypt_value(plain: str) -> str:
    if not plain:
        return ""
    return _get_fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_value(encrypted: str) -> str:
    """Decrypt a value; fail loudly instead of returning the ciphertext."""
    if not encrypted:
        return ""
    try:
        return _get_fernet().decrypt(encrypted.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError) as exc:
        raise DecryptionError(
            "Could not decrypt a stored value. Check FIELD_ENCRYPTION_KEY (or SECRET_KEY, "
            "when the dedicated key is not set): it must be the key the value was saved with."
        ) from exc


class EncryptedTextField(models.TextField):
    """TextField that stores a Fernet token in the database and exposes plain text in Python.

    It is a TextField (not a CharField) because the token is longer than the plain value.
    Limit the plain length with ``MaxLengthValidator`` on the model field.
    """

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None:
            return value
        return encrypt_value(value)

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        return decrypt_value(value)
