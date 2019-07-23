# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
"""Resources required for Raw Keyrings."""

import logging
import os

import attr
import six

from attr.validators import instance_of, optional
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from aws_encryption_sdk.exceptions import GenerateKeyError
from aws_encryption_sdk.identifiers import EncryptionKeyType, KeyringTraceFlag, WrappingAlgorithm
from aws_encryption_sdk.internal.crypto.wrapping_keys import EncryptedData, WrappingKey
from aws_encryption_sdk.internal.formatting.deserialize import deserialize_wrapped_key
from aws_encryption_sdk.internal.formatting.serialize import serialize_raw_master_key_prefix, serialize_wrapped_key
from aws_encryption_sdk.key_providers.raw import RawMasterKey
from aws_encryption_sdk.keyring.base import Keyring
from aws_encryption_sdk.materials_managers import DecryptionMaterials, EncryptionMaterials
from aws_encryption_sdk.structures import EncryptedDataKey, KeyringTrace, MasterKeyInfo, RawDataKey

try:  # Python 3.5.0 and 3.5.1 have incompatible typing modules
    from typing import Iterable  # noqa pylint: disable=unused-import
except ImportError:  # pragma: no cover
    # We only actually need these imports when running the mypy checks
    pass

_LOGGER = logging.getLogger(__name__)


def generate_data_key(
    encryption_materials,  # type: EncryptionMaterials
    key_provider,  # type: MasterKeyInfo
):
    # type: (...) -> bool
    """Generates plaintext data key for the keyring.

    :param encryption_materials: Encryption materials for the keyring to modify.
    :type encryption_materials: aws_encryption_sdk.materials_managers.EncryptionMaterials
    :param key_provider: Information about the key in the keyring.
    :type key_provider: MasterKeyInfo
    :return bytes: Plaintext data key
    """
    # Generate data key
    plaintext_data_key = os.urandom(encryption_materials.algorithm.kdf_input_len)

    # Check if data key is generated
    if not plaintext_data_key or plaintext_data_key is None:
        raise GenerateKeyError("Unable to generate data encryption key.")

    # Create a keyring trace
    keyring_trace = KeyringTrace(wrapping_key=key_provider, flags={KeyringTraceFlag.WRAPPING_KEY_GENERATED_DATA_KEY})

    # plaintext_data_key to RawDataKey
    data_encryption_key = RawDataKey(key_provider=key_provider, data_key=plaintext_data_key)

    # Add generated data key to encryption_materials
    encryption_materials.add_data_encryption_key(data_encryption_key, keyring_trace)

    return True


@attr.s
class RawAESKeyring(Keyring):
    """Public class for Raw AES Keyring.

    :param str key_namespace: String defining the keyring.
    :param bytes key_name: Key ID
    :param bytes wrapping_key: Encryption key with which to wrap plaintext data key.
    :param wrapping_algorithm: Wrapping Algorithm with which to wrap plaintext data key.
    :type wrapping_algorithm: WrappingAlgorithm
    """

    key_namespace = attr.ib(validator=instance_of(six.string_types))
    key_name = attr.ib(validator=instance_of(six.binary_type))
    _wrapping_key = attr.ib(repr=False, validator=instance_of(six.binary_type))
    _wrapping_algorithm = attr.ib(repr=False, validator=instance_of(WrappingAlgorithm))

    @staticmethod
    def _get_key_info_prefix(key_namespace, key_name, wrapping_key):
        # type: (str, bytes, WrappingKey) -> six.binary_type
        """Helper function to get key info prefix

        :param str key_namespace: String defining the keyring.
        :param bytes key_name: Key ID
        :param wrapping_key: Encryption key with which to wrap plaintext data key.
        :type wrapping_key: WrappingKey
        :return: Serialized key_info prefix
        :rtype: bytes
        """
        key_info_prefix = serialize_raw_master_key_prefix(
            RawMasterKey(provider_id=key_namespace, key_id=key_name, wrapping_key=wrapping_key)
        )
        return key_info_prefix

    def __attrs_post_init__(self):
        # type: () -> None
        """Prepares initial values not handled by attrs."""
        self._key_provider = MasterKeyInfo(provider_id=self.key_namespace, key_info=self.key_name)

        self._wrapping_key_structure = WrappingKey(
            wrapping_algorithm=self._wrapping_algorithm,
            wrapping_key=self._wrapping_key,
            wrapping_key_type=EncryptionKeyType.SYMMETRIC,
        )

        self._key_info_prefix = self._get_key_info_prefix(
            key_namespace=self.key_namespace, key_name=self.key_name, wrapping_key=self._wrapping_key_structure
        )

    def on_encrypt(self, encryption_materials):
        # type: (EncryptionMaterials) -> EncryptionMaterials
        """Generate a data key if not present and encrypt it using any available wrapping key

        :param encryption_materials: Encryption materials for the keyring to modify
        :type encryption_materials: aws_encryption_sdk.materials_managers.EncryptionMaterials
        :returns: Optionally modified encryption materials
        :rtype: aws_encryption_sdk.materials_managers.EncryptionMaterials
        """
        if encryption_materials.data_encryption_key is None:
            plaintext_generated = generate_data_key(
                encryption_materials=encryption_materials, key_provider=self._key_provider
            )

            # Check if data key exists
            if not plaintext_generated:
                raise GenerateKeyError("Unable to generate data encryption key.")

        # Encrypt data key
        encrypted_wrapped_key = self._wrapping_key_structure.encrypt(
            plaintext_data_key=encryption_materials.data_encryption_key.data_key,
            encryption_context=encryption_materials.encryption_context,
        )

        # Check if encryption is successful
        if encrypted_wrapped_key is None:
            return encryption_materials

        # EncryptedData to EncryptedDataKey
        try:
            encrypted_data_key = serialize_wrapped_key(
                key_provider=self._key_provider,
                wrapping_algorithm=self._wrapping_algorithm,
                wrapping_key_id=self.key_name,
                encrypted_wrapped_key=encrypted_wrapped_key,
            )
        except Exception:  # pylint: disable=broad-except
            error_message = "Raw AES Keyring unable to encrypt data key"
            _LOGGER.exception(error_message)
            return encryption_materials

        # Update Keyring Trace
        keyring_trace = KeyringTrace(
            wrapping_key=encrypted_data_key.key_provider, flags={KeyringTraceFlag.WRAPPING_KEY_ENCRYPTED_DATA_KEY}
        )

        # Add encrypted data key to encryption_materials
        encryption_materials.add_encrypted_data_key(encrypted_data_key=encrypted_data_key, keyring_trace=keyring_trace)
        return encryption_materials

    def on_decrypt(self, decryption_materials, encrypted_data_keys):
        # type: (DecryptionMaterials, Iterable[EncryptedDataKey]) -> DecryptionMaterials
        """Attempt to decrypt the encrypted data keys.

        :param decryption_materials: Decryption materials for the keyring to modify
        :type decryption_materials: aws_encryption_sdk.materials_managers.DecryptionMaterials
        :param encrypted_data_keys: List of encrypted data keys
        :type: List of `aws_encryption_sdk.structures.EncryptedDataKey`
        :returns: Optionally modified decryption materials
        :rtype: aws_encryption_sdk.materials_managers.DecryptionMaterials
        """
        if decryption_materials.data_encryption_key is not None:
            print("Data key already present")
            return decryption_materials

        # Decrypt data key
        expected_key_info_len = len(self._key_info_prefix) + self._wrapping_algorithm.algorithm.iv_len
        print("expected_key_info_len=", expected_key_info_len)
        for key in encrypted_data_keys:

            if decryption_materials.data_encryption_key is not None:
                return decryption_materials

            if (
                key.key_provider.provider_id != self._key_provider.provider_id
                or len(key.key_provider.key_info) != expected_key_info_len
                or not key.key_provider.key_info.startswith(self._key_info_prefix)
            ):
                print("If condition matched")
                continue

            # Wrapped EncryptedDataKey to deserialized EncryptedData
            encrypted_wrapped_key = deserialize_wrapped_key(
                wrapping_algorithm=self._wrapping_algorithm, wrapping_key_id=key.key_provider.key_info,
                wrapped_encrypted_key=key
            )

            # EncryptedData to raw key string
            try:
                plaintext_data_key = self._wrapping_key_structure.decrypt(
                    encrypted_wrapped_data_key=encrypted_wrapped_key,
                    encryption_context=decryption_materials.encryption_context,
                )

            except Exception:  # pylint: disable=broad-except
                error_message = "Raw AES Keyring unable to decrypt data key"
                _LOGGER.exception(error_message)
                return decryption_materials

            # Create a keyring trace
            keyring_trace = KeyringTrace(
                wrapping_key=self._key_provider, flags={KeyringTraceFlag.WRAPPING_KEY_DECRYPTED_DATA_KEY}
            )

            print("Keyring trace made")

            # Update decryption materials
            data_encryption_key = RawDataKey(
                key_provider=MasterKeyInfo(provider_id=self._key_provider.provider_id, key_info=self.key_name),
                data_key=plaintext_data_key,
            )
            decryption_materials.add_data_encryption_key(data_encryption_key, keyring_trace)
            print("Done")

        return decryption_materials


@attr.s
class RawRSAKeyring(Keyring):
    """Public class for Raw RSA Keyring.

    :param str key_namespace: String defining the keyring ID
    :param bytes key_name: Key ID
    :param _private_wrapping_key: Private encryption key with which to wrap plaintext data key (optional)
    :type _private_wrapping_key: RSAPrivateKey
    :param _public_wrapping_key: Public encryption key with which to wrap plaintext data key (optional)
    :type _public_wrapping_key: RSAPublicKey
    :param wrapping_algorithm: Wrapping Algorithm with which to wrap plaintext data key
    :type wrapping_algorithm: WrappingAlgorithm
    :param key_provider: Complete information about the key in the keyring
    :type key_provider: MasterKeyInfo
    """

    key_namespace = attr.ib(validator=instance_of(six.string_types))
    key_name = attr.ib(validator=instance_of(six.binary_type))
    _wrapping_algorithm = attr.ib(repr=False, validator=instance_of(WrappingAlgorithm))
    _private_wrapping_key = attr.ib(default=None, repr=False, validator=optional(instance_of(rsa.RSAPrivateKey)))
    _public_wrapping_key = attr.ib(default=None, repr=False, validator=optional(instance_of(rsa.RSAPublicKey)))

    @classmethod
    def fromPEMEncoding(
        cls,
        key_namespace,  # type: str
        key_name,  # type: bytes
        wrapping_algorithm,  # type: WrappingAlgorithm
        public_encoded_key=None,  # type: bytes
        private_encoded_key=None,  # type: bytes
        password=None,  # type: bytes
    ):
        # type: (...) -> RawRSAKeyring
        """Generate a raw RSA keyring using a key with PEM Encoding

        :param str key_namespace: String defining the keyring ID
        :param bytes key_name: Key ID
        :param wrapping_algorithm: Wrapping Algorithm with which to wrap plaintext data key
        :type wrapping_algorithm: WrappingAlgorithm
        :param bytes public_encoded_key: PEM encoded public key (optional)
        :param bytes private_encoded_key: PEM encoded private key (optional)
        :param bytes password: Password to load private key (optional)
        :return: Calls RawRSAKeyring class with required parameters
        """
        loaded_private_wrapping_key = loaded_public_wrapping_key = None
        if private_encoded_key is not None:
            loaded_private_wrapping_key = serialization.load_pem_private_key(
                data=private_encoded_key, password=password, backend=default_backend()
            )
        if public_encoded_key is not None:
            loaded_public_wrapping_key = serialization.load_pem_public_key(
                data=public_encoded_key, backend=default_backend()
            )
        if public_encoded_key is None and private_encoded_key is None:
            raise TypeError("At least one of public key or private key must be provided.")

        return cls(
            key_namespace=key_namespace,
            key_name=key_name,
            wrapping_algorithm=wrapping_algorithm,
            private_wrapping_key=loaded_private_wrapping_key,
            public_wrapping_key=loaded_public_wrapping_key,
        )

    @classmethod
    def fromDEREncoding(
        cls,
        key_namespace,  # type: str
        key_name,  # type: bytes
        wrapping_algorithm,  # type: WrappingAlgorithm
        public_encoded_key=None,  # type: bytes
        private_encoded_key=None,  # type: bytes
        password=None,  # type: bytes
    ):
        """Generate a raw RSA keyring using a key with DER Encoding

        :param str key_namespace: String defining the keyring ID
        :param bytes key_name: Key ID
        :param wrapping_algorithm: Wrapping Algorithm with which to wrap plaintext data key
        :type wrapping_algorithm: WrappingAlgorithm
        :param bytes public_encoded_key: DER encoded public key (optional)
        :param bytes private_encoded_key: DER encoded private key (optional)
        :param password: Password to load private key (optional)
        :return: Calls RawRSAKeyring class with required parameters
        """
        loaded_private_wrapping_key = loaded_public_wrapping_key = None
        if private_encoded_key is not None:
            loaded_private_wrapping_key = serialization.load_der_private_key(
                data=private_encoded_key, password=password, backend=default_backend()
            )
        if public_encoded_key is not None:
            loaded_public_wrapping_key = serialization.load_der_public_key(
                data=public_encoded_key, backend=default_backend()
            )
        if public_encoded_key is None and private_encoded_key is None:
            raise TypeError("At least one of public key or private key must be provided.")

        return cls(
            key_namespace=key_namespace,
            key_name=key_name,
            wrapping_algorithm=wrapping_algorithm,
            private_wrapping_key=loaded_private_wrapping_key,
            public_wrapping_key=loaded_public_wrapping_key,
        )

    def __attrs_post_init__(self):
        # type: () -> None
        """Prepares initial values not handled by attrs."""
        self._key_provider = MasterKeyInfo(provider_id=self.key_namespace, key_info=self.key_name)
        if self._private_wrapping_key is not None:
            self._public_wrapping_key = self._private_wrapping_key.public_key()

    def on_encrypt(self, encryption_materials):
        # type: (EncryptionMaterials) -> EncryptionMaterials
        """Generate a data key if not present and encrypt it using any available wrapping key.

        :param encryption_materials: Encryption materials for the keyring to modify.
        :type encryption_materials: aws_encryption_sdk.materials_managers.EncryptionMaterials
        :returns: Optionally modified encryption materials.
        :rtype: aws_encryption_sdk.materials_managers.EncryptionMaterials
        """
        if encryption_materials.data_encryption_key is None:
            plaintext_generated = generate_data_key(
                encryption_materials=encryption_materials, key_provider=self._key_provider
            )

            # Check if data key exists
            if not plaintext_generated:
                raise GenerateKeyError("Unable to generate data encryption key.")

        if self._public_wrapping_key is None:
            return encryption_materials

        # Encrypt data key
        try:
            encrypted_wrapped_key = EncryptedData(
                iv=None,
                ciphertext=self._public_wrapping_key.encrypt(
                    plaintext=encryption_materials.data_encryption_key.data_key,
                    padding=self._wrapping_algorithm.padding
                ),
                tag=None,
            )
        except Exception:  # pylint: disable=broad-except
            error_message = "Raw RSA Keyring unable to encrypt data key"
            _LOGGER.exception(error_message)
            return encryption_materials

        # EncryptedData to EncryptedDataKey
        encrypted_data_key = serialize_wrapped_key(
            key_provider=self._key_provider,
            wrapping_algorithm=self._wrapping_algorithm,
            wrapping_key_id=self.key_name,
            encrypted_wrapped_key=encrypted_wrapped_key,
        )

        # Update Keyring Trace
        keyring_trace = KeyringTrace(
            wrapping_key=encrypted_data_key.key_provider, flags={KeyringTraceFlag.WRAPPING_KEY_ENCRYPTED_DATA_KEY}
        )

        # Add encrypted data key to encryption_materials
        encryption_materials.add_encrypted_data_key(encrypted_data_key=encrypted_data_key, keyring_trace=keyring_trace)

        return encryption_materials

    def on_decrypt(self, decryption_materials, encrypted_data_keys):
        # type: (DecryptionMaterials, Iterable[EncryptedDataKey]) -> DecryptionMaterials
        """Attempt to decrypt the encrypted data keys.

        :param decryption_materials: Decryption materials for the keyring to modify.
        :type decryption_materials: aws_encryption_sdk.materials_managers.DecryptionMaterials
        :param encrypted_data_keys: List of encrypted data keys.
        :type: List of `aws_encryption_sdk.structures.EncryptedDataKey`
        :returns: Optionally modified decryption materials.
        :rtype: aws_encryption_sdk.materials_managers.DecryptionMaterials
        """
        if self._private_wrapping_key is None:
            return decryption_materials

        # Decrypt data key
        for key in encrypted_data_keys:
            if decryption_materials.data_encryption_key is not None:
                return decryption_materials
            if key.key_provider != self._key_provider:
                continue
            # Wrapped EncryptedDataKey to deserialized EncryptedData
            encrypted_wrapped_key = deserialize_wrapped_key(
                wrapping_algorithm=self._wrapping_algorithm, wrapping_key_id=key.key_provider.key_info,
                wrapped_encrypted_key=key
            )
            try:
                plaintext_data_key = self._private_wrapping_key.decrypt(
                    ciphertext=encrypted_wrapped_key.ciphertext, padding=self._wrapping_algorithm.padding
                )
            except Exception:  # pylint: disable=broad-except
                error_message = "Raw RSA Keyring unable to decrypt data key"
                _LOGGER.exception(error_message)
                continue

            # Create a keyring trace
            keyring_trace = KeyringTrace(
                wrapping_key=self._key_provider, flags={KeyringTraceFlag.WRAPPING_KEY_DECRYPTED_DATA_KEY}
            )

            # Update decryption materials
            data_encryption_key = RawDataKey(
                key_provider=MasterKeyInfo(provider_id=self._key_provider.provider_id, key_info=self.key_name),
                data_key=plaintext_data_key,
            )
            decryption_materials.add_data_encryption_key(data_encryption_key, keyring_trace)

        return decryption_materials
