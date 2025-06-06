# -*- coding: utf-8 -*-
#
# Copyright (c) 2016, Yanis Guenane <yanis+ansible@guenane.org>
# Copyright (c) 2020, Felix Fontein <felix@fontein.de>
# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import absolute_import, division, print_function


__metaclass__ = type


import abc
import binascii
import traceback

from ansible.module_utils import six
from ansible.module_utils.basic import missing_required_lib
from ansible.module_utils.common.text.converters import to_native, to_text
from ansible_collections.community.crypto.plugins.module_utils.argspec import (
    ArgumentSpec,
)
from ansible_collections.community.crypto.plugins.module_utils.crypto.basic import (
    OpenSSLBadPassphraseError,
    OpenSSLObjectError,
)
from ansible_collections.community.crypto.plugins.module_utils.crypto.cryptography_crl import (
    REVOCATION_REASON_MAP,
)
from ansible_collections.community.crypto.plugins.module_utils.crypto.cryptography_support import (
    cryptography_get_basic_constraints,
    cryptography_get_name,
    cryptography_key_needs_digest_for_signing,
    cryptography_name_to_oid,
    cryptography_parse_key_usage_params,
    cryptography_parse_relative_distinguished_name,
)
from ansible_collections.community.crypto.plugins.module_utils.crypto.module_backends.csr_info import (
    get_csr_info,
)
from ansible_collections.community.crypto.plugins.module_utils.crypto.support import (
    load_certificate_request,
    load_privatekey,
    parse_name_field,
    parse_ordered_name_field,
    select_message_digest,
)
from ansible_collections.community.crypto.plugins.module_utils.version import (
    LooseVersion,
)


MINIMAL_CRYPTOGRAPHY_VERSION = "1.3"

CRYPTOGRAPHY_IMP_ERR = None
try:
    import cryptography
    import cryptography.exceptions
    import cryptography.hazmat.backends
    import cryptography.hazmat.primitives.hashes
    import cryptography.hazmat.primitives.serialization
    import cryptography.x509
    import cryptography.x509.oid

    CRYPTOGRAPHY_VERSION = LooseVersion(cryptography.__version__)
except ImportError:
    CRYPTOGRAPHY_IMP_ERR = traceback.format_exc()
    CRYPTOGRAPHY_FOUND = False
else:
    CRYPTOGRAPHY_FOUND = True
    CRYPTOGRAPHY_MUST_STAPLE_NAME = cryptography.x509.oid.ObjectIdentifier(
        "1.3.6.1.5.5.7.1.24"
    )
    CRYPTOGRAPHY_MUST_STAPLE_VALUE = b"\x30\x03\x02\x01\x05"


class CertificateSigningRequestError(OpenSSLObjectError):
    pass


# From the object called `module`, only the following properties are used:
#
#  - module.params[]
#  - module.warn(msg: str)
#  - module.fail_json(msg: str, **kwargs)


@six.add_metaclass(abc.ABCMeta)
class CertificateSigningRequestBackend(object):
    def __init__(self, module, backend):
        self.module = module
        self.backend = backend
        self.digest = module.params["digest"]
        self.privatekey_path = module.params["privatekey_path"]
        self.privatekey_content = module.params["privatekey_content"]
        if self.privatekey_content is not None:
            self.privatekey_content = self.privatekey_content.encode("utf-8")
        self.privatekey_passphrase = module.params["privatekey_passphrase"]
        self.version = module.params["version"]
        self.subjectAltName = module.params["subject_alt_name"]
        self.subjectAltName_critical = module.params["subject_alt_name_critical"]
        self.keyUsage = module.params["key_usage"]
        self.keyUsage_critical = module.params["key_usage_critical"]
        self.extendedKeyUsage = module.params["extended_key_usage"]
        self.extendedKeyUsage_critical = module.params["extended_key_usage_critical"]
        self.basicConstraints = module.params["basic_constraints"]
        self.basicConstraints_critical = module.params["basic_constraints_critical"]
        self.ocspMustStaple = module.params["ocsp_must_staple"]
        self.ocspMustStaple_critical = module.params["ocsp_must_staple_critical"]
        self.name_constraints_permitted = (
            module.params["name_constraints_permitted"] or []
        )
        self.name_constraints_excluded = (
            module.params["name_constraints_excluded"] or []
        )
        self.name_constraints_critical = module.params["name_constraints_critical"]
        self.create_subject_key_identifier = module.params[
            "create_subject_key_identifier"
        ]
        self.subject_key_identifier = module.params["subject_key_identifier"]
        self.authority_key_identifier = module.params["authority_key_identifier"]
        self.authority_cert_issuer = module.params["authority_cert_issuer"]
        self.authority_cert_serial_number = module.params[
            "authority_cert_serial_number"
        ]
        self.crl_distribution_points = module.params["crl_distribution_points"]
        self.csr = None
        self.privatekey = None

        if (
            self.create_subject_key_identifier
            and self.subject_key_identifier is not None
        ):
            module.fail_json(
                msg="subject_key_identifier cannot be specified if create_subject_key_identifier is true"
            )

        self.ordered_subject = False
        self.subject = [
            ("C", module.params["country_name"]),
            ("ST", module.params["state_or_province_name"]),
            ("L", module.params["locality_name"]),
            ("O", module.params["organization_name"]),
            ("OU", module.params["organizational_unit_name"]),
            ("CN", module.params["common_name"]),
            ("emailAddress", module.params["email_address"]),
        ]
        self.subject = [(entry[0], entry[1]) for entry in self.subject if entry[1]]

        try:
            if module.params["subject"]:
                self.subject = self.subject + parse_name_field(
                    module.params["subject"], "subject"
                )
            if module.params["subject_ordered"]:
                if self.subject:
                    raise CertificateSigningRequestError(
                        "subject_ordered cannot be combined with any other subject field"
                    )
                self.subject = parse_ordered_name_field(
                    module.params["subject_ordered"], "subject_ordered"
                )
                self.ordered_subject = True
        except ValueError as exc:
            raise CertificateSigningRequestError(to_native(exc))

        self.using_common_name_for_san = False
        if not self.subjectAltName and module.params["use_common_name_for_san"]:
            for sub in self.subject:
                if sub[0] in ("commonName", "CN"):
                    self.subjectAltName = ["DNS:%s" % sub[1]]
                    self.using_common_name_for_san = True
                    break

        if self.subject_key_identifier is not None:
            try:
                self.subject_key_identifier = binascii.unhexlify(
                    self.subject_key_identifier.replace(":", "")
                )
            except Exception as e:
                raise CertificateSigningRequestError(
                    "Cannot parse subject_key_identifier: {0}".format(e)
                )

        if self.authority_key_identifier is not None:
            try:
                self.authority_key_identifier = binascii.unhexlify(
                    self.authority_key_identifier.replace(":", "")
                )
            except Exception as e:
                raise CertificateSigningRequestError(
                    "Cannot parse authority_key_identifier: {0}".format(e)
                )

        self.existing_csr = None
        self.existing_csr_bytes = None

        self.diff_before = self._get_info(None)
        self.diff_after = self._get_info(None)

    def _get_info(self, data):
        if data is None:
            return dict()
        try:
            result = get_csr_info(
                self.module,
                self.backend,
                data,
                validate_signature=False,
                prefer_one_fingerprint=True,
            )
            result["can_parse_csr"] = True
            return result
        except Exception:
            return dict(can_parse_csr=False)

    @abc.abstractmethod
    def generate_csr(self):
        """(Re-)Generate CSR."""
        pass

    @abc.abstractmethod
    def get_csr_data(self):
        """Return bytes for self.csr."""
        pass

    def set_existing(self, csr_bytes):
        """Set existing CSR bytes. None indicates that the CSR does not exist."""
        self.existing_csr_bytes = csr_bytes
        self.diff_after = self.diff_before = self._get_info(self.existing_csr_bytes)

    def has_existing(self):
        """Query whether an existing CSR is/has been there."""
        return self.existing_csr_bytes is not None

    def _ensure_private_key_loaded(self):
        """Load the provided private key into self.privatekey."""
        if self.privatekey is not None:
            return
        try:
            self.privatekey = load_privatekey(
                path=self.privatekey_path,
                content=self.privatekey_content,
                passphrase=self.privatekey_passphrase,
                backend=self.backend,
            )
        except OpenSSLBadPassphraseError as exc:
            raise CertificateSigningRequestError(exc)

    @abc.abstractmethod
    def _check_csr(self):
        """Check whether provided parameters, assuming self.existing_csr and self.privatekey have been populated."""
        pass

    def needs_regeneration(self):
        """Check whether a regeneration is necessary."""
        if self.existing_csr_bytes is None:
            return True
        try:
            self.existing_csr = load_certificate_request(
                None, content=self.existing_csr_bytes, backend=self.backend
            )
        except Exception:
            return True
        self._ensure_private_key_loaded()
        return not self._check_csr()

    def dump(self, include_csr):
        """Serialize the object into a dictionary."""
        result = {
            "privatekey": self.privatekey_path,
            "subject": self.subject,
            "subjectAltName": self.subjectAltName,
            "keyUsage": self.keyUsage,
            "extendedKeyUsage": self.extendedKeyUsage,
            "basicConstraints": self.basicConstraints,
            "ocspMustStaple": self.ocspMustStaple,
            "name_constraints_permitted": self.name_constraints_permitted,
            "name_constraints_excluded": self.name_constraints_excluded,
        }
        # Get hold of CSR bytes
        csr_bytes = self.existing_csr_bytes
        if self.csr is not None:
            csr_bytes = self.get_csr_data()
        self.diff_after = self._get_info(csr_bytes)
        if include_csr:
            # Store result
            result["csr"] = csr_bytes.decode("utf-8") if csr_bytes else None

        result["diff"] = dict(
            before=self.diff_before,
            after=self.diff_after,
        )
        return result


def parse_crl_distribution_points(module, crl_distribution_points):
    result = []
    for index, parse_crl_distribution_point in enumerate(crl_distribution_points):
        try:
            params = dict(
                full_name=None,
                relative_name=None,
                crl_issuer=None,
                reasons=None,
            )
            if parse_crl_distribution_point["full_name"] is not None:
                if not parse_crl_distribution_point["full_name"]:
                    raise OpenSSLObjectError("full_name must not be empty")
                params["full_name"] = [
                    cryptography_get_name(name, "full name")
                    for name in parse_crl_distribution_point["full_name"]
                ]
            if parse_crl_distribution_point["relative_name"] is not None:
                if not parse_crl_distribution_point["relative_name"]:
                    raise OpenSSLObjectError("relative_name must not be empty")
                try:
                    params["relative_name"] = (
                        cryptography_parse_relative_distinguished_name(
                            parse_crl_distribution_point["relative_name"]
                        )
                    )
                except Exception:
                    # If cryptography's version is < 1.6, the error is probably caused by that
                    if CRYPTOGRAPHY_VERSION < LooseVersion("1.6"):
                        raise OpenSSLObjectError(
                            "Cannot specify relative_name for cryptography < 1.6"
                        )
                    raise
            if parse_crl_distribution_point["crl_issuer"] is not None:
                if not parse_crl_distribution_point["crl_issuer"]:
                    raise OpenSSLObjectError("crl_issuer must not be empty")
                params["crl_issuer"] = [
                    cryptography_get_name(name, "CRL issuer")
                    for name in parse_crl_distribution_point["crl_issuer"]
                ]
            if parse_crl_distribution_point["reasons"] is not None:
                reasons = []
                for reason in parse_crl_distribution_point["reasons"]:
                    reasons.append(REVOCATION_REASON_MAP[reason])
                params["reasons"] = frozenset(reasons)
            result.append(cryptography.x509.DistributionPoint(**params))
        except (OpenSSLObjectError, ValueError) as e:
            raise OpenSSLObjectError(
                "Error while parsing CRL distribution point #{index}: {error}".format(
                    index=index, error=e
                )
            )
    return result


# Implementation with using cryptography
class CertificateSigningRequestCryptographyBackend(CertificateSigningRequestBackend):
    def __init__(self, module):
        super(CertificateSigningRequestCryptographyBackend, self).__init__(
            module, "cryptography"
        )
        self.cryptography_backend = cryptography.hazmat.backends.default_backend()
        if self.version != 1:
            module.warn(
                "The cryptography backend only supports version 1. (The only valid value according to RFC 2986.)"
            )

        if self.crl_distribution_points:
            self.crl_distribution_points = parse_crl_distribution_points(
                module, self.crl_distribution_points
            )

    def generate_csr(self):
        """(Re-)Generate CSR."""
        self._ensure_private_key_loaded()

        csr = cryptography.x509.CertificateSigningRequestBuilder()
        try:
            csr = csr.subject_name(
                cryptography.x509.Name(
                    [
                        cryptography.x509.NameAttribute(
                            cryptography_name_to_oid(entry[0]), to_text(entry[1])
                        )
                        for entry in self.subject
                    ]
                )
            )
        except ValueError as e:
            raise CertificateSigningRequestError(e)

        if self.subjectAltName:
            csr = csr.add_extension(
                cryptography.x509.SubjectAlternativeName(
                    [cryptography_get_name(name) for name in self.subjectAltName]
                ),
                critical=self.subjectAltName_critical,
            )

        if self.keyUsage:
            params = cryptography_parse_key_usage_params(self.keyUsage)
            csr = csr.add_extension(
                cryptography.x509.KeyUsage(**params), critical=self.keyUsage_critical
            )

        if self.extendedKeyUsage:
            usages = [
                cryptography_name_to_oid(usage) for usage in self.extendedKeyUsage
            ]
            csr = csr.add_extension(
                cryptography.x509.ExtendedKeyUsage(usages),
                critical=self.extendedKeyUsage_critical,
            )

        if self.basicConstraints:
            params = {}
            ca, path_length = cryptography_get_basic_constraints(self.basicConstraints)
            csr = csr.add_extension(
                cryptography.x509.BasicConstraints(ca, path_length),
                critical=self.basicConstraints_critical,
            )

        if self.ocspMustStaple:
            try:
                # This only works with cryptography >= 2.1
                csr = csr.add_extension(
                    cryptography.x509.TLSFeature(
                        [cryptography.x509.TLSFeatureType.status_request]
                    ),
                    critical=self.ocspMustStaple_critical,
                )
            except AttributeError:
                csr = csr.add_extension(
                    cryptography.x509.UnrecognizedExtension(
                        CRYPTOGRAPHY_MUST_STAPLE_NAME, CRYPTOGRAPHY_MUST_STAPLE_VALUE
                    ),
                    critical=self.ocspMustStaple_critical,
                )

        if self.name_constraints_permitted or self.name_constraints_excluded:
            try:
                csr = csr.add_extension(
                    cryptography.x509.NameConstraints(
                        [
                            cryptography_get_name(name, "name constraints permitted")
                            for name in self.name_constraints_permitted
                        ]
                        or None,
                        [
                            cryptography_get_name(name, "name constraints excluded")
                            for name in self.name_constraints_excluded
                        ]
                        or None,
                    ),
                    critical=self.name_constraints_critical,
                )
            except TypeError as e:
                raise OpenSSLObjectError(
                    "Error while parsing name constraint: {0}".format(e)
                )

        if self.create_subject_key_identifier:
            csr = csr.add_extension(
                cryptography.x509.SubjectKeyIdentifier.from_public_key(
                    self.privatekey.public_key()
                ),
                critical=False,
            )
        elif self.subject_key_identifier is not None:
            csr = csr.add_extension(
                cryptography.x509.SubjectKeyIdentifier(self.subject_key_identifier),
                critical=False,
            )

        if (
            self.authority_key_identifier is not None
            or self.authority_cert_issuer is not None
            or self.authority_cert_serial_number is not None
        ):
            issuers = None
            if self.authority_cert_issuer is not None:
                issuers = [
                    cryptography_get_name(n, "authority cert issuer")
                    for n in self.authority_cert_issuer
                ]
            csr = csr.add_extension(
                cryptography.x509.AuthorityKeyIdentifier(
                    self.authority_key_identifier,
                    issuers,
                    self.authority_cert_serial_number,
                ),
                critical=False,
            )

        if self.crl_distribution_points:
            csr = csr.add_extension(
                cryptography.x509.CRLDistributionPoints(self.crl_distribution_points),
                critical=False,
            )

        digest = None
        if cryptography_key_needs_digest_for_signing(self.privatekey):
            digest = select_message_digest(self.digest)
            if digest is None:
                raise CertificateSigningRequestError(
                    'Unsupported digest "{0}"'.format(self.digest)
                )
        try:
            self.csr = csr.sign(self.privatekey, digest, self.cryptography_backend)
        except TypeError as e:
            if (
                str(e) == "Algorithm must be a registered hash algorithm."
                and digest is None
            ):
                self.module.fail_json(
                    msg="Signing with Ed25519 and Ed448 keys requires cryptography 2.8 or newer."
                )
            raise
        except UnicodeError as e:
            # This catches IDNAErrors, which happens when a bad name is passed as a SAN
            # (https://github.com/ansible-collections/community.crypto/issues/105).
            # For older cryptography versions, this is handled by idna, which raises
            # an idna.core.IDNAError. Later versions of cryptography deprecated and stopped
            # requiring idna, whence we cannot easily handle this error. Fortunately, in
            # most versions of idna, IDNAError extends UnicodeError. There is only version
            # 2.3 where it extends Exception instead (see
            # https://github.com/kjd/idna/commit/ebefacd3134d0f5da4745878620a6a1cba86d130
            # and then
            # https://github.com/kjd/idna/commit/ea03c7b5db7d2a99af082e0239da2b68aeea702a).
            msg = "Error while creating CSR: {0}\n".format(e)
            if self.using_common_name_for_san:
                self.module.fail_json(
                    msg=msg
                    + "This is probably caused because the Common Name is used as a SAN."
                    " Specifying use_common_name_for_san=false might fix this."
                )
            self.module.fail_json(
                msg=msg
                + "This is probably caused by an invalid Subject Alternative DNS Name."
            )

    def get_csr_data(self):
        """Return bytes for self.csr."""
        return self.csr.public_bytes(
            cryptography.hazmat.primitives.serialization.Encoding.PEM
        )

    def _check_csr(self):
        """Check whether provided parameters, assuming self.existing_csr and self.privatekey have been populated."""

        def _check_subject(csr):
            subject = [
                (cryptography_name_to_oid(entry[0]), to_text(entry[1]))
                for entry in self.subject
            ]
            current_subject = [(sub.oid, sub.value) for sub in csr.subject]
            if self.ordered_subject:
                return subject == current_subject
            else:
                return set(subject) == set(current_subject)

        def _find_extension(extensions, exttype):
            return next(
                (ext for ext in extensions if isinstance(ext.value, exttype)), None
            )

        def _check_subjectAltName(extensions):
            current_altnames_ext = _find_extension(
                extensions, cryptography.x509.SubjectAlternativeName
            )
            current_altnames = (
                [to_text(altname) for altname in current_altnames_ext.value]
                if current_altnames_ext
                else []
            )
            altnames = (
                [
                    to_text(cryptography_get_name(altname))
                    for altname in self.subjectAltName
                ]
                if self.subjectAltName
                else []
            )
            if set(altnames) != set(current_altnames):
                return False
            if altnames:
                if current_altnames_ext.critical != self.subjectAltName_critical:
                    return False
            return True

        def _check_keyUsage(extensions):
            current_keyusage_ext = _find_extension(
                extensions, cryptography.x509.KeyUsage
            )
            if not self.keyUsage:
                return current_keyusage_ext is None
            elif current_keyusage_ext is None:
                return False
            params = cryptography_parse_key_usage_params(self.keyUsage)
            for param in params:
                if getattr(current_keyusage_ext.value, "_" + param) != params[param]:
                    return False
            if current_keyusage_ext.critical != self.keyUsage_critical:
                return False
            return True

        def _check_extenededKeyUsage(extensions):
            current_usages_ext = _find_extension(
                extensions, cryptography.x509.ExtendedKeyUsage
            )
            current_usages = (
                [str(usage) for usage in current_usages_ext.value]
                if current_usages_ext
                else []
            )
            usages = (
                [
                    str(cryptography_name_to_oid(usage))
                    for usage in self.extendedKeyUsage
                ]
                if self.extendedKeyUsage
                else []
            )
            if set(current_usages) != set(usages):
                return False
            if usages:
                if current_usages_ext.critical != self.extendedKeyUsage_critical:
                    return False
            return True

        def _check_basicConstraints(extensions):
            bc_ext = _find_extension(extensions, cryptography.x509.BasicConstraints)
            current_ca = bc_ext.value.ca if bc_ext else False
            current_path_length = bc_ext.value.path_length if bc_ext else None
            ca, path_length = cryptography_get_basic_constraints(self.basicConstraints)
            # Check CA flag
            if ca != current_ca:
                return False
            # Check path length
            if path_length != current_path_length:
                return False
            # Check criticality
            if self.basicConstraints:
                return (
                    bc_ext is not None
                    and bc_ext.critical == self.basicConstraints_critical
                )
            else:
                return bc_ext is None

        def _check_ocspMustStaple(extensions):
            try:
                # This only works with cryptography >= 2.1
                tlsfeature_ext = _find_extension(
                    extensions, cryptography.x509.TLSFeature
                )
                has_tlsfeature = True
            except AttributeError:
                tlsfeature_ext = next(
                    (
                        ext
                        for ext in extensions
                        if ext.value.oid == CRYPTOGRAPHY_MUST_STAPLE_NAME
                    ),
                    None,
                )
                has_tlsfeature = False
            if self.ocspMustStaple:
                if (
                    not tlsfeature_ext
                    or tlsfeature_ext.critical != self.ocspMustStaple_critical
                ):
                    return False
                if has_tlsfeature:
                    return (
                        cryptography.x509.TLSFeatureType.status_request
                        in tlsfeature_ext.value
                    )
                else:
                    return tlsfeature_ext.value.value == CRYPTOGRAPHY_MUST_STAPLE_VALUE
            else:
                return tlsfeature_ext is None

        def _check_nameConstraints(extensions):
            current_nc_ext = _find_extension(
                extensions, cryptography.x509.NameConstraints
            )
            current_nc_perm = (
                [
                    to_text(altname)
                    for altname in current_nc_ext.value.permitted_subtrees or []
                ]
                if current_nc_ext
                else []
            )
            current_nc_excl = (
                [
                    to_text(altname)
                    for altname in current_nc_ext.value.excluded_subtrees or []
                ]
                if current_nc_ext
                else []
            )
            nc_perm = [
                to_text(cryptography_get_name(altname, "name constraints permitted"))
                for altname in self.name_constraints_permitted
            ]
            nc_excl = [
                to_text(cryptography_get_name(altname, "name constraints excluded"))
                for altname in self.name_constraints_excluded
            ]
            if set(nc_perm) != set(current_nc_perm) or set(nc_excl) != set(
                current_nc_excl
            ):
                return False
            if nc_perm or nc_excl:
                if current_nc_ext.critical != self.name_constraints_critical:
                    return False
            return True

        def _check_subject_key_identifier(extensions):
            ext = _find_extension(extensions, cryptography.x509.SubjectKeyIdentifier)
            if (
                self.create_subject_key_identifier
                or self.subject_key_identifier is not None
            ):
                if not ext or ext.critical:
                    return False
                if self.create_subject_key_identifier:
                    digest = cryptography.x509.SubjectKeyIdentifier.from_public_key(
                        self.privatekey.public_key()
                    ).digest
                    return ext.value.digest == digest
                else:
                    return ext.value.digest == self.subject_key_identifier
            else:
                return ext is None

        def _check_authority_key_identifier(extensions):
            ext = _find_extension(extensions, cryptography.x509.AuthorityKeyIdentifier)
            if (
                self.authority_key_identifier is not None
                or self.authority_cert_issuer is not None
                or self.authority_cert_serial_number is not None
            ):
                if not ext or ext.critical:
                    return False
                aci = None
                csr_aci = None
                if self.authority_cert_issuer is not None:
                    aci = [
                        to_text(cryptography_get_name(n, "authority cert issuer"))
                        for n in self.authority_cert_issuer
                    ]
                if ext.value.authority_cert_issuer is not None:
                    csr_aci = [to_text(n) for n in ext.value.authority_cert_issuer]
                return (
                    ext.value.key_identifier == self.authority_key_identifier
                    and csr_aci == aci
                    and ext.value.authority_cert_serial_number
                    == self.authority_cert_serial_number
                )
            else:
                return ext is None

        def _check_crl_distribution_points(extensions):
            ext = _find_extension(extensions, cryptography.x509.CRLDistributionPoints)
            if self.crl_distribution_points is None:
                return ext is None
            if not ext:
                return False
            return list(ext.value) == self.crl_distribution_points

        def _check_extensions(csr):
            extensions = csr.extensions
            return (
                _check_subjectAltName(extensions)
                and _check_keyUsage(extensions)
                and _check_extenededKeyUsage(extensions)
                and _check_basicConstraints(extensions)
                and _check_ocspMustStaple(extensions)
                and _check_subject_key_identifier(extensions)
                and _check_authority_key_identifier(extensions)
                and _check_nameConstraints(extensions)
                and _check_crl_distribution_points(extensions)
            )

        def _check_signature(csr):
            if not csr.is_signature_valid:
                return False
            # To check whether public key of CSR belongs to private key,
            # encode both public keys and compare PEMs.
            key_a = csr.public_key().public_bytes(
                cryptography.hazmat.primitives.serialization.Encoding.PEM,
                cryptography.hazmat.primitives.serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            key_b = self.privatekey.public_key().public_bytes(
                cryptography.hazmat.primitives.serialization.Encoding.PEM,
                cryptography.hazmat.primitives.serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            return key_a == key_b

        return (
            _check_subject(self.existing_csr)
            and _check_extensions(self.existing_csr)
            and _check_signature(self.existing_csr)
        )


def select_backend(module, backend):
    if backend == "auto":
        # Detection what is possible
        can_use_cryptography = (
            CRYPTOGRAPHY_FOUND
            and CRYPTOGRAPHY_VERSION >= LooseVersion(MINIMAL_CRYPTOGRAPHY_VERSION)
        )

        # Try cryptography
        if can_use_cryptography:
            backend = "cryptography"

        # Success?
        if backend == "auto":
            module.fail_json(
                msg=(
                    "Cannot detect any of the required Python libraries "
                    "cryptography (>= {0})"
                ).format(MINIMAL_CRYPTOGRAPHY_VERSION)
            )

    if backend == "cryptography":
        if not CRYPTOGRAPHY_FOUND:
            module.fail_json(
                msg=missing_required_lib(
                    "cryptography >= {0}".format(MINIMAL_CRYPTOGRAPHY_VERSION)
                ),
                exception=CRYPTOGRAPHY_IMP_ERR,
            )
        return backend, CertificateSigningRequestCryptographyBackend(module)
    else:
        raise Exception("Unsupported value for backend: {0}".format(backend))


def get_csr_argument_spec():
    return ArgumentSpec(
        argument_spec=dict(
            digest=dict(type="str", default="sha256"),
            privatekey_path=dict(type="path"),
            privatekey_content=dict(type="str", no_log=True),
            privatekey_passphrase=dict(type="str", no_log=True),
            version=dict(type="int", default=1, choices=[1]),
            subject=dict(type="dict"),
            subject_ordered=dict(type="list", elements="dict"),
            country_name=dict(type="str", aliases=["C", "countryName"]),
            state_or_province_name=dict(
                type="str", aliases=["ST", "stateOrProvinceName"]
            ),
            locality_name=dict(type="str", aliases=["L", "localityName"]),
            organization_name=dict(type="str", aliases=["O", "organizationName"]),
            organizational_unit_name=dict(
                type="str", aliases=["OU", "organizationalUnitName"]
            ),
            common_name=dict(type="str", aliases=["CN", "commonName"]),
            email_address=dict(type="str", aliases=["E", "emailAddress"]),
            subject_alt_name=dict(
                type="list", elements="str", aliases=["subjectAltName"]
            ),
            subject_alt_name_critical=dict(
                type="bool", default=False, aliases=["subjectAltName_critical"]
            ),
            use_common_name_for_san=dict(
                type="bool", default=True, aliases=["useCommonNameForSAN"]
            ),
            key_usage=dict(type="list", elements="str", aliases=["keyUsage"]),
            key_usage_critical=dict(
                type="bool", default=False, aliases=["keyUsage_critical"]
            ),
            extended_key_usage=dict(
                type="list", elements="str", aliases=["extKeyUsage", "extendedKeyUsage"]
            ),
            extended_key_usage_critical=dict(
                type="bool",
                default=False,
                aliases=["extKeyUsage_critical", "extendedKeyUsage_critical"],
            ),
            basic_constraints=dict(
                type="list", elements="str", aliases=["basicConstraints"]
            ),
            basic_constraints_critical=dict(
                type="bool", default=False, aliases=["basicConstraints_critical"]
            ),
            ocsp_must_staple=dict(
                type="bool", default=False, aliases=["ocspMustStaple"]
            ),
            ocsp_must_staple_critical=dict(
                type="bool", default=False, aliases=["ocspMustStaple_critical"]
            ),
            name_constraints_permitted=dict(type="list", elements="str"),
            name_constraints_excluded=dict(type="list", elements="str"),
            name_constraints_critical=dict(type="bool", default=False),
            create_subject_key_identifier=dict(type="bool", default=False),
            subject_key_identifier=dict(type="str"),
            authority_key_identifier=dict(type="str"),
            authority_cert_issuer=dict(type="list", elements="str"),
            authority_cert_serial_number=dict(type="int"),
            crl_distribution_points=dict(
                type="list",
                elements="dict",
                options=dict(
                    full_name=dict(type="list", elements="str"),
                    relative_name=dict(type="list", elements="str"),
                    crl_issuer=dict(type="list", elements="str"),
                    reasons=dict(
                        type="list",
                        elements="str",
                        choices=[
                            "key_compromise",
                            "ca_compromise",
                            "affiliation_changed",
                            "superseded",
                            "cessation_of_operation",
                            "certificate_hold",
                            "privilege_withdrawn",
                            "aa_compromise",
                        ],
                    ),
                ),
                mutually_exclusive=[("full_name", "relative_name")],
                required_one_of=[("full_name", "relative_name", "crl_issuer")],
            ),
            select_crypto_backend=dict(
                type="str", default="auto", choices=["auto", "cryptography"]
            ),
        ),
        required_together=[
            ["authority_cert_issuer", "authority_cert_serial_number"],
        ],
        mutually_exclusive=[
            ["privatekey_path", "privatekey_content"],
            ["subject", "subject_ordered"],
        ],
        required_one_of=[
            ["privatekey_path", "privatekey_content"],
        ],
    )
