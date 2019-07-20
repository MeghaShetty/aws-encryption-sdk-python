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
    from typing import Iterable, Union  # noqa pylint: disable=unused-import
except ImportError:  # pragma: no cover
    # We only actually need these imports when running the mypy checks
    pass


def get_key_info_prefix(key_namespace, key_name, wrapping_key):
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


def generate_data_key(
    encryption_materials,  # type: EncryptionMaterials
    key_provider,  # type: MasterKeyInfo
):
    # type: (...) -> bytes
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
    if not plaintext_data_key:
        raise GenerateKeyError("Unable to generate data encryption key.")

    # Create a keyring trace
    keyring_trace = KeyringTrace(wrapping_key=key_provider, flags={KeyringTraceFlag.WRAPPING_KEY_GENERATED_DATA_KEY})

    # plaintext_data_key to RawDataKey
    data_encryption_key = RawDataKey(key_provider=key_provider, data_key=plaintext_data_key)

    # Add generated data key to encryption_materials
    encryption_materials.add_data_encryption_key(data_encryption_key, keyring_trace)

    return plaintext_data_key


@attr.s
class RawAESKeyring(Keyring):
    """Public class for Raw AES Keyring.

    :param str key_namespace: String defining the keyring.
    :param bytes key_name: Key ID
    :param bytes wrapping_key: Encryption key with which to wrap plaintext data key.
    :param wrapping_algorithm: Wrapping Algorithm with which to wrap plaintext data key.
    :type wrapping_algorithm: WrappingAlgorithm
    """

    key_namespace = attr.ib(validator=attr.validators.instance_of(six.string_types))
    key_name = attr.ib(validator=attr.validators.instance_of(six.binary_type))
    _wrapping_key = attr.ib(repr=False, validator=attr.validators.instance_of(six.binary_type))
    _wrapping_algorithm = attr.ib(repr=False, validator=attr.validators.instance_of(WrappingAlgorithm))

    def __attrs_post_init__(self):
        # type: () -> None
        """Prepares initial values not handled by attrs."""
        self._key_provider = MasterKeyInfo(provider_id=self.key_namespace, key_info=self.key_name)

        self._wrapping_key_structure = WrappingKey(
            wrapping_algorithm=self._wrapping_algorithm,
            wrapping_key=self._wrapping_key,
            wrapping_key_type=EncryptionKeyType.SYMMETRIC,
        )

        self._key_info_prefix = get_key_info_prefix(
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
        if not encryption_materials.data_encryption_key:
            plaintext_data_key = generate_data_key(
                encryption_materials=encryption_materials, key_provider=self._key_provider
            )
        else:
            plaintext_data_key = encryption_materials.data_encryption_key.data_key

        # Encrypt data key
        encrypted_wrapped_key = self._wrapping_key_structure.encrypt(
            plaintext_data_key=plaintext_data_key, encryption_context=encryption_materials.encryption_context
        )

        # EncryptedData to EncryptedDataKey
        encrypted_data_key = serialize_wrapped_key(
            key_provider=self._key_provider,
            wrapping_algorithm=self._wrapping_algorithm,
            wrapping_key_id=self.key_name,
            encrypted_wrapped_key=encrypted_wrapped_key,
        )

        # Update Keyring Trace
        if encrypted_data_key:
            keyring_trace = KeyringTrace(
                wrapping_key=encrypted_data_key.key_provider, flags={KeyringTraceFlag.WRAPPING_KEY_ENCRYPTED_DATA_KEY}
            )

            # Add encrypted data key to encryption_materials
            encryption_materials.add_encrypted_data_key(
                encrypted_data_key=encrypted_data_key, keyring_trace=keyring_trace
            )
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
        if decryption_materials.data_encryption_key:
            return decryption_materials

        # Decrypt data key
        expected_key_info_len = len(self._key_info_prefix) + self._wrapping_algorithm.algorithm.iv_len
        for key in encrypted_data_keys:
            if (
                key.key_provider.provider_id == self._key_provider.provider_id
                and len(key.key_provider.key_info) == expected_key_info_len
                and key.key_provider.key_info.startswith(self._key_info_prefix)
            ):
                # Wrapped EncryptedDataKey to deserialized EncryptedData
                encrypted_wrapped_key = deserialize_wrapped_key(
                    wrapping_algorithm=self._wrapping_algorithm,
                    wrapping_key_id=self.key_name,
                    wrapped_encrypted_key=key,
                )
                # EncryptedData to raw key string
                try:
                    plaintext_data_key = self._wrapping_key_structure.decrypt(
                        encrypted_wrapped_data_key=encrypted_wrapped_key,
                        encryption_context=decryption_materials.encryption_context,
                    )

                except Exception as error:  # pylint: disable=broad-except
                    logger = logging.getLogger()
                    logger.error(error.__class__.__name__, ":", str(error))
                    return decryption_materials

                if plaintext_data_key:
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

                if decryption_materials.data_encryption_key:
                    return decryption_materials

        return decryption_materials


@attr.s
class RawRSAKeyring(Keyring):
    """Public class for Raw RSA Keyring.

    :param str key_namespace: String defining the keyring ID
    :param bytes key_name: Key ID
    :param wrapping_key: Encryption key with which to wrap plaintext data key
    :type wrapping_key: object
    :param wrapping_algorithm: Wrapping Algorithm with which to wrap plaintext data key
    :type wrapping_algorithm: WrappingAlgorithm
    :param key_provider: Complete information about the key in the keyring
    :type key_provider: MasterKeyInfo
    """

    key_namespace = attr.ib(validator=attr.validators.instance_of(six.string_types))
    key_name = attr.ib(validator=attr.validators.instance_of(six.binary_type))
    # _wrapping_key = attr.ib(repr=False, validator=attr.validators.in_([rsa.RSAPrivateKey, rsa.RSAPublicKey]))
    # ----- need to figure out how to do this correctly
    _wrapping_key = attr.ib(repr=False, validator=attr.validators.instance_of(object))
    _wrapping_algorithm = attr.ib(repr=False, validator=attr.validators.instance_of(WrappingAlgorithm))

    @classmethod
    def fromPEMEncoding(cls):
        key_namespace = attr.ib(validator=attr.validators.instance_of(six.string_types))
        key_name = attr.ib(validator=attr.validators.instance_of(six.binary_type))
        _encoded_key = attr.ib(validator=attr.validators.instance_of(six.binary_type))
        _password = attr.ib(validator=attr.validators.optional(attr.validators.instance_of(six.binary_type)))
        _wrapping_algorithm = attr.ib(repr=False, validator=attr.validators.instance_of(WrappingAlgorithm))
        if _password:
            loaded_wrapping_key = serialization.load_pem_private_key(
                data=_encoded_key, password=_password, backend=default_backend()
            )
        else:
            loaded_wrapping_key = serialization.load_pem_public_key(data=_encoded_key, backend=default_backend())
        return RawRSAKeyring(
            key_namespace=key_namespace,
            key_name=key_name,
            wrapping_key=loaded_wrapping_key,
            wrapping_algorithm=_wrapping_algorithm,
        )

    @classmethod
    def fromDEREncoding(cls):
        key_namespace = attr.ib(validator=attr.validators.instance_of(six.string_types))
        key_name = attr.ib(validator=attr.validators.instance_of(six.binary_type))
        _encoded_key = attr.ib(validator=attr.validators.instance_of(six.binary_type))
        _password = attr.ib(validator=attr.validators.optional(attr.validators.instance_of(six.binary_type)))
        _wrapping_algorithm = attr.ib(repr=False, validator=attr.validators.instance_of(WrappingAlgorithm))
        if _password:
            loaded_wrapping_key = serialization.load_der_private_key(
                data=_encoded_key, password=_password, backend=default_backend()
            )
        else:
            loaded_wrapping_key = serialization.load_der_public_key(data=_encoded_key, backend=default_backend())
        return RawRSAKeyring(
            key_namespace=key_namespace,
            key_name=key_name,
            wrapping_key=loaded_wrapping_key,
            wrapping_algorithm=_wrapping_algorithm,
        )

    def __attrs_post_init__(self):
        # type: () -> None
        """Prepares initial values not handled by attrs."""
        self._key_provider = MasterKeyInfo(provider_id=self.key_namespace, key_info=self.key_name)

    def on_encrypt(self, encryption_materials):
        # type: (EncryptionMaterials) -> EncryptionMaterials
        """Generate a data key if not present and encrypt it using any available wrapping key.

        :param encryption_materials: Encryption materials for the keyring to modify.
        :type encryption_materials: aws_encryption_sdk.materials_managers.EncryptionMaterials
        :returns: Optionally modified encryption materials.
        :rtype: aws_encryption_sdk.materials_managers.EncryptionMaterials
        """
        if not encryption_materials.data_encryption_key:
            plaintext_data_key = generate_data_key(
                encryption_materials=encryption_materials, key_provider=self._key_provider
            )
        else:
            plaintext_data_key = encryption_materials.data_encryption_key.data_key
        if isinstance(self._wrapping_key, rsa.RSAPublicKey):

            # Encrypt data key
            encrypted_wrapped_key = EncryptedData(
                iv=None,
                ciphertext=self._wrapping_key.encrypt(
                    plaintext=plaintext_data_key, padding=self._wrapping_algorithm.padding
                ),
                tag=None,
            )
        else:
            # Encrypt data key
            encrypted_wrapped_key = EncryptedData(
                iv=None,
                ciphertext=self._wrapping_key.public_key().encrypt(
                    plaintext=plaintext_data_key, padding=self._wrapping_algorithm.padding
                ),
                tag=None,
            )

        # EncryptedData to EncryptedDataKey
        encrypted_data_key = serialize_wrapped_key(
            key_provider=self._key_provider,
            wrapping_algorithm=self._wrapping_algorithm,
            wrapping_key_id=self.key_name,
            encrypted_wrapped_key=encrypted_wrapped_key,
        )

        # Update Keyring Trace
        if encrypted_data_key:
            keyring_trace = KeyringTrace(
                wrapping_key=encrypted_data_key.key_provider, flags={KeyringTraceFlag.WRAPPING_KEY_ENCRYPTED_DATA_KEY}
            )

            # Add encrypted data key to encryption_materials
            encryption_materials.add_encrypted_data_key(
                encrypted_data_key=encrypted_data_key, keyring_trace=keyring_trace
            )

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
        if isinstance(self._wrapping_key, rsa.RSAPrivateKey):
            # Decrypt data key
            for key in encrypted_data_keys:
                if key.key_provider == self._key_provider:
                    # Wrapped EncryptedDataKey to deserialized EncryptedData
                    encrypted_wrapped_key = deserialize_wrapped_key(
                        wrapping_algorithm=self._wrapping_algorithm,
                        wrapping_key_id=self.key_name,
                        wrapped_encrypted_key=key,
                    )
                    try:
                        plaintext_data_key = self._wrapping_key.decrypt(
                            ciphertext=encrypted_wrapped_key.ciphertext, padding=self._wrapping_algorithm.padding
                        )
                    except Exception as error:  # pylint: disable=broad-except
                        logger = logging.getLogger()
                        logger.error(error.__class__.__name__, ":", str(error))
                        return decryption_materials

                    if plaintext_data_key:
                        # Create a keyring trace
                        keyring_trace = KeyringTrace(
                            wrapping_key=self._key_provider, flags={KeyringTraceFlag.WRAPPING_KEY_DECRYPTED_DATA_KEY}
                        )

                        # Update decryption materials
                        data_encryption_key = RawDataKey(
                            key_provider=MasterKeyInfo(
                                provider_id=self._key_provider.provider_id, key_info=self.key_name
                            ),
                            data_key=plaintext_data_key,
                        )
                        decryption_materials.add_data_encryption_key(data_encryption_key, keyring_trace)
                    if decryption_materials.data_encryption_key:
                        return decryption_materials

        return decryption_materials
