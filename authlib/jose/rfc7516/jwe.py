from collections import OrderedDict
from copy import deepcopy

from authlib.common.encoding import (
    to_bytes, urlsafe_b64encode, json_b64encode, to_unicode
)
from authlib.jose.rfc7516.models import JWEAlgorithmWithTagAwareKeyAgreement, JWESharedHeader, JWEHeader
from authlib.jose.util import (
    extract_header,
    extract_segment, ensure_dict,
)
from authlib.jose.errors import (
    DecodeError,
    MissingAlgorithmError,
    UnsupportedAlgorithmError,
    MissingEncryptionAlgorithmError,
    UnsupportedEncryptionAlgorithmError,
    UnsupportedCompressionAlgorithmError,
    InvalidHeaderParameterNameError, InvalidAlgorithmForMultipleRecipientsMode,
)


class JsonWebEncryption(object):
    #: Registered Header Parameter Names defined by Section 4.1
    REGISTERED_HEADER_PARAMETER_NAMES = frozenset([
        'alg', 'enc', 'zip',
        'jku', 'jwk', 'kid',
        'x5u', 'x5c', 'x5t', 'x5t#S256',
        'typ', 'cty', 'crit'
    ])

    ALG_REGISTRY = {}
    ENC_REGISTRY = {}
    ZIP_REGISTRY = {}

    def __init__(self, algorithms=None, private_headers=None):
        self._algorithms = algorithms
        self._private_headers = private_headers

    @classmethod
    def register_algorithm(cls, algorithm):
        """Register an algorithm for ``alg`` or ``enc`` or ``zip`` of JWE."""
        if not algorithm or algorithm.algorithm_type != 'JWE':
            raise ValueError(
                'Invalid algorithm for JWE, {!r}'.format(algorithm))

        if algorithm.algorithm_location == 'alg':
            cls.ALG_REGISTRY[algorithm.name] = algorithm
        elif algorithm.algorithm_location == 'enc':
            cls.ENC_REGISTRY[algorithm.name] = algorithm
        elif algorithm.algorithm_location == 'zip':
            cls.ZIP_REGISTRY[algorithm.name] = algorithm

    def serialize_compact(self, protected, payload, key, sender_key=None):
        """Generate a JWE Compact Serialization. The JWE Compact Serialization
        represents encrypted content as a compact, URL-safe string.  This
        string is:

            BASE64URL(UTF8(JWE Protected Header)) || '.' ||
            BASE64URL(JWE Encrypted Key) || '.' ||
            BASE64URL(JWE Initialization Vector) || '.' ||
            BASE64URL(JWE Ciphertext) || '.' ||
            BASE64URL(JWE Authentication Tag)

        Only one recipient is supported by the JWE Compact Serialization and
        it provides no syntax to represent JWE Shared Unprotected Header, JWE
        Per-Recipient Unprotected Header, or JWE AAD values.

        :param protected: A dict of protected header
        :param payload: A string/dict of payload
        :param key: Public key used to encrypt payload
        :param sender_key: Sender's private key in case JWEAlgorithmWithTagAwareKeyAgreement is used
        :return: byte
        """

        # step 1: Prepare algorithms & key
        alg = self.get_header_alg(protected)
        enc = self.get_header_enc(protected)
        zip_alg = self.get_header_zip(protected)

        self._validate_sender_key(sender_key, alg)
        self._validate_private_headers(protected, alg)

        key = prepare_key(alg, protected, key)
        if sender_key is not None:
            sender_key = alg.prepare_key(sender_key)

        # self._post_validate_header(protected, algorithm)

        # step 2: Generate a random Content Encryption Key (CEK)
        # use enc_alg.generate_cek() in scope of upcoming .wrap or .generate_keys_and_prepare_headers call

        # step 3: Encrypt the CEK with the recipient's public key
        if isinstance(alg, JWEAlgorithmWithTagAwareKeyAgreement) and alg.key_size is not None:
            # For a JWE algorithm with tag-aware key agreement in case key agreement with key wrapping mode is used:
            # Defer key agreement with key wrapping until authentication tag is computed
            prep = alg.generate_keys_and_prepare_headers(enc, key, sender_key)
            epk = prep['epk']
            cek = prep['cek']
            protected.update(prep['header'])
        else:
            # In any other case:
            # Keep the normal steps order defined by RFC 7516
            if isinstance(alg, JWEAlgorithmWithTagAwareKeyAgreement):
                wrapped = alg.wrap(enc, protected, key, sender_key)
            else:
                wrapped = alg.wrap(enc, protected, key)
            cek = wrapped['cek']
            ek = wrapped['ek']
            if 'header' in wrapped:
                protected.update(wrapped['header'])

        # step 4: Generate a random JWE Initialization Vector
        iv = enc.generate_iv()

        # step 5: Let the Additional Authenticated Data encryption parameter
        # be ASCII(BASE64URL(UTF8(JWE Protected Header)))
        protected_segment = json_b64encode(protected)
        aad = to_bytes(protected_segment, 'ascii')

        # step 6: compress message if required
        if zip_alg:
            msg = zip_alg.compress(to_bytes(payload))
        else:
            msg = to_bytes(payload)

        # step 7: perform encryption
        ciphertext, tag = enc.encrypt(msg, aad, iv, cek)

        if isinstance(alg, JWEAlgorithmWithTagAwareKeyAgreement) and alg.key_size is not None:
            # For a JWE algorithm with tag-aware key agreement in case key agreement with key wrapping mode is used:
            # Perform key agreement with key wrapping deferred at step 3
            wrapped = alg.agree_upon_key_and_wrap_cek(enc, protected, key, sender_key, epk, cek, tag)
            ek = wrapped['ek']

        # step 8: build resulting message
        return b'.'.join([
            protected_segment,
            urlsafe_b64encode(ek),
            urlsafe_b64encode(iv),
            urlsafe_b64encode(ciphertext),
            urlsafe_b64encode(tag)
        ])

    def serialize_json(self, header_obj, payload, keys, sender_key=None):
        if isinstance(keys, tuple):  # tuple instead of list
            keys = list(keys)
        if not isinstance(keys, list):  # single key
            keys = [keys]

        if not keys:
            raise ValueError("No keys have been provided")

        header_obj = deepcopy(header_obj)

        shared_header = JWESharedHeader.from_dict(header_obj)

        recipients = header_obj.get('recipients')
        if recipients is None:
            recipients = [{} for _ in keys]
        for i in range(len(recipients)):
            if recipients[i] is None:
                recipients[i] = {}
            if recipients[i].get('header') is None:
                recipients[i]['header'] = {}

        jwe_aad = header_obj.get('aad')

        if len(keys) != len(recipients):
            raise ValueError("Count of recipient keys {} does not equal to count of recipients {}"
                             .format(len(keys), len(recipients)))

        # step 1: Prepare algorithms & key
        alg = self.get_header_alg(shared_header)
        enc = self.get_header_enc(shared_header)
        zip_alg = self.get_header_zip(shared_header)

        self._validate_sender_key(sender_key, alg)
        self._validate_private_headers(shared_header, alg)
        for recipient in recipients:
            self._validate_private_headers(recipient['header'], alg)

        for i in range(len(keys)):
            keys[i] = prepare_key(alg, recipients[i]['header'], keys[i])
        if sender_key is not None:
            sender_key = alg.prepare_key(sender_key)

        # self._post_validate_header(protected, algorithm)

        # step 2: Generate a random Content Encryption Key (CEK)
        # use enc_alg.generate_cek() in scope of upcoming .wrap or .generate_keys_and_prepare_headers call

        # step 3: Encrypt the CEK with the recipient's public key
        preset = alg.generate_preset(enc, keys[0])
        if 'cek' in preset:
            cek = preset['cek']
        else:
            cek = None
        if len(keys) > 1 and cek is None:
            raise InvalidAlgorithmForMultipleRecipientsMode(alg.name)
        if 'header' in preset:
            shared_header.update_protected(preset['header'])

        if isinstance(alg, JWEAlgorithmWithTagAwareKeyAgreement) and alg.key_size is not None:
            # For a JWE algorithm with tag-aware key agreement in case key agreement with key wrapping mode is used:
            # Defer key agreement with key wrapping until authentication tag is computed
            epks = []
            for i in range(len(keys)):
                prep = alg.generate_keys_and_prepare_headers(enc, keys[i], sender_key, preset)
                if cek is None:
                    cek = prep['cek']
                epks.append(prep['epk'])
                recipients[i]['header'].update(prep['header'])
        else:
            # In any other case:
            # Keep the normal steps order defined by RFC 7516
            for i in range(len(keys)):
                if isinstance(alg, JWEAlgorithmWithTagAwareKeyAgreement):
                    wrapped = alg.wrap(enc, shared_header, keys[i], sender_key, preset)
                else:
                    wrapped = alg.wrap(enc, shared_header, keys[i], preset)
                if cek is None:
                    cek = wrapped['cek']
                recipients[i]['encrypted_key'] = wrapped['ek']
                if 'header' in wrapped:
                    recipients[i]['header'].update(wrapped['header'])

        # step 4: Generate a random JWE Initialization Vector
        iv = enc.generate_iv()

        # step 5: Compute the Encoded Protected Header value
        # BASE64URL(UTF8(JWE Protected Header)). If the JWE Protected Header
        # is not present, let this value be the empty string.
        # Let the Additional Authenticated Data encryption parameter be
        # ASCII(Encoded Protected Header). However, if a JWE AAD value is
        # present, instead let the Additional Authenticated Data encryption
        # parameter be ASCII(Encoded Protected Header || '.' || BASE64URL(JWE AAD)).
        aad = json_b64encode(shared_header.protected) if shared_header.protected else b''
        if jwe_aad is not None:
           aad += b'.' + urlsafe_b64encode(jwe_aad)
        aad = to_bytes(aad, 'ascii')

        # step 6: compress message if required
        if zip_alg:
            msg = zip_alg.compress(to_bytes(payload))
        else:
            msg = to_bytes(payload)

        # step 7: perform encryption
        ciphertext, tag = enc.encrypt(msg, aad, iv, cek)

        if isinstance(alg, JWEAlgorithmWithTagAwareKeyAgreement) and alg.key_size is not None:
            # For a JWE algorithm with tag-aware key agreement in case key agreement with key wrapping mode is used:
            # Perform key agreement with key wrapping deferred at step 3
            for i in range(len(keys)):
                wrapped = alg.agree_upon_key_and_wrap_cek(enc, shared_header, keys[i], sender_key, epks[i], cek, tag)
                recipients[i]['encrypted_key'] = wrapped['ek']

        # step 8: build resulting message
        obj = OrderedDict()

        if shared_header.protected:
            obj['protected'] = to_unicode(json_b64encode(shared_header.protected))

        if shared_header.unprotected:
            obj['unprotected'] = shared_header.unprotected

        for recipient in recipients:
            if not recipient['header']:
                del recipient['header']
            if recipient['encrypted_key']:
                recipient['encrypted_key'] = to_unicode(urlsafe_b64encode(recipient['encrypted_key']))
        obj['recipients'] = recipients

        if jwe_aad is not None:
            obj['aad'] = to_unicode(urlsafe_b64encode(jwe_aad))

        obj['iv'] = to_unicode(urlsafe_b64encode(iv))

        obj['ciphertext'] = to_unicode(urlsafe_b64encode(ciphertext))

        obj['tag'] = to_unicode(urlsafe_b64encode(tag))

        return obj

    def deserialize_compact(self, s, key, decode=None, sender_key=None):
        """Exact JWS Compact Serialization, and validate with the given key.

        :param s: text of JWS Compact Serialization
        :param key: private key used to decrypt payload
        :param decode: a function to decode plaintext data
        :param sender_key: sender's public key in case JWEAlgorithmWithTagAwareKeyAgreement is used
        :return: dict
        """
        try:
            s = to_bytes(s)
            protected_s, ek_s, iv_s, ciphertext_s, tag_s = s.rsplit(b'.')
        except ValueError:
            raise DecodeError('Not enough segments')

        protected = extract_header(protected_s, DecodeError)
        ek = extract_segment(ek_s, DecodeError, 'encryption key')
        iv = extract_segment(iv_s, DecodeError, 'initialization vector')
        ciphertext = extract_segment(ciphertext_s, DecodeError, 'ciphertext')
        tag = extract_segment(tag_s, DecodeError, 'authentication tag')

        alg = self.get_header_alg(protected)
        enc = self.get_header_enc(protected)
        zip_alg = self.get_header_zip(protected)

        self._validate_sender_key(sender_key, alg)
        self._validate_private_headers(protected, alg)

        key = prepare_key(alg, protected, key)
        if sender_key is not None:
            sender_key = alg.prepare_key(sender_key)

        if isinstance(alg, JWEAlgorithmWithTagAwareKeyAgreement):
            # For a JWE algorithm with tag-aware key agreement:
            if alg.key_size is not None:
                # In case key agreement with key wrapping mode is used:
                # Provide authentication tag to .unwrap method
                cek = alg.unwrap(enc, ek, protected, key, sender_key, tag)
            else:
                # Otherwise, don't provide authentication tag to .unwrap method
                cek = alg.unwrap(enc, ek, protected, key, sender_key)
        else:
            # For any other JWE algorithm:
            # Don't provide authentication tag to .unwrap method
            cek = alg.unwrap(enc, ek, protected, key)

        aad = to_bytes(protected_s, 'ascii')
        msg = enc.decrypt(ciphertext, aad, iv, tag, cek)

        if zip_alg:
            payload = zip_alg.decompress(to_bytes(msg))
        else:
            payload = msg

        if decode:
            payload = decode(payload)
        return {'header': protected, 'payload': payload}

    def deserialize_json(self, obj, key, decode=None, sender_key=None):
        obj = ensure_dict(obj, 'JWE')
        obj = deepcopy(obj)

        if 'protected' in obj:
            protected = extract_header(to_bytes(obj['protected']), DecodeError)
        else:
            protected = None

        unprotected = obj.get('unprotected')

        recipients = obj['recipients']
        for recipient in recipients:
            recipient['encrypted_key'] = extract_segment(
                to_bytes(recipient['encrypted_key']), DecodeError, 'encrypted key')

        if 'aad' in obj:
            jwe_aad = extract_segment(to_bytes(obj['aad']), DecodeError, 'JWE AAD')
        else:
            jwe_aad = None

        iv = extract_segment(to_bytes(obj['iv']), DecodeError, 'initialization vector')

        ciphertext = extract_segment(to_bytes(obj['ciphertext']), DecodeError, 'ciphertext')

        tag = extract_segment(to_bytes(obj['tag']), DecodeError, 'authentication tag')

        shared_header = JWESharedHeader(protected, unprotected)

        alg = self.get_header_alg(shared_header)
        enc = self.get_header_enc(shared_header)
        zip_alg = self.get_header_zip(shared_header)

        self._validate_sender_key(sender_key, alg)
        self._validate_private_headers(shared_header, alg)
        for recipient in recipients:
            self._validate_private_headers(recipient['header'], alg)

        key = alg.prepare_key(key)
        if sender_key is not None:
            sender_key = alg.prepare_key(sender_key)

        def _unwrap_with_sender_key_and_tag(ek, header):
            return alg.unwrap(enc, ek, header, key, sender_key, tag)

        def _unwrap_with_sender_key_and_without_tag(ek, header):
            return alg.unwrap(enc, ek, header, key, sender_key)

        def _unwrap_without_sender_key_and_tag(ek, header):
            return alg.unwrap(enc, ek, header, key)

        def _unwrap_for_matching_recipient(unwrap_func):
            if key.kid is not None:
                for recipient in recipients:
                    if recipient['header'].get('kid') == key.kid:
                        header = JWEHeader(protected, unprotected, recipient['header'])
                        return unwrap_func(recipient['encrypted_key'], header)

            # Since no explicit match has been found, iterate over all the recipients
            for recipient in recipients:
                header = JWEHeader(protected, unprotected, recipient['header'])
                try:
                    return unwrap_func(recipient['encrypted_key'], header)
                except Exception as e:
                    error = e
            else:
                raise error

        if isinstance(alg, JWEAlgorithmWithTagAwareKeyAgreement):
            # For a JWE algorithm with tag-aware key agreement:
            if alg.key_size is not None:
                # In case key agreement with key wrapping mode is used:
                # Provide authentication tag to .unwrap method
                cek = _unwrap_for_matching_recipient(_unwrap_with_sender_key_and_tag)
            else:
                # Otherwise, don't provide authentication tag to .unwrap method
                cek = _unwrap_for_matching_recipient(_unwrap_with_sender_key_and_without_tag)
        else:
            # For any other JWE algorithm:
            # Don't provide authentication tag to .unwrap method
            cek = _unwrap_for_matching_recipient(_unwrap_without_sender_key_and_tag)

        aad = to_bytes(obj.get('protected', ''))
        if 'aad' in obj:
           aad += b'.' + to_bytes(obj['aad'])
        aad = to_bytes(aad, 'ascii')

        msg = enc.decrypt(ciphertext, aad, iv, tag, cek)

        if zip_alg:
            payload = zip_alg.decompress(to_bytes(msg))
        else:
            payload = msg

        if decode:
            payload = decode(payload)

        for recipient in recipients:
            del recipient['encrypted_key']

        header = {}
        if protected:
            header['protected'] = protected
        if unprotected:
            header['unprotected'] = unprotected
        header['recipients'] = recipients
        if jwe_aad is not None:
            header['aad'] = jwe_aad

        return {
            'header': header,
            'payload': payload
        }

    def get_header_alg(self, header):
        if 'alg' not in header:
            raise MissingAlgorithmError()

        alg = header['alg']
        if self._algorithms and alg not in self._algorithms:
            raise UnsupportedAlgorithmError()
        if alg not in self.ALG_REGISTRY:
            raise UnsupportedAlgorithmError()
        return self.ALG_REGISTRY[alg]

    def get_header_enc(self, header):
        if 'enc' not in header:
            raise MissingEncryptionAlgorithmError()
        enc = header['enc']
        if self._algorithms and enc not in self._algorithms:
            raise UnsupportedEncryptionAlgorithmError()
        if enc not in self.ENC_REGISTRY:
            raise UnsupportedEncryptionAlgorithmError()
        return self.ENC_REGISTRY[enc]

    def get_header_zip(self, header):
        if 'zip' in header:
            z = header['zip']
            if self._algorithms and z not in self._algorithms:
                raise UnsupportedCompressionAlgorithmError()
            if z not in self.ZIP_REGISTRY:
                raise UnsupportedCompressionAlgorithmError()
            return self.ZIP_REGISTRY[z]

    def _validate_sender_key(self, sender_key, alg):
        if isinstance(alg, JWEAlgorithmWithTagAwareKeyAgreement):
            if sender_key is None:
                raise ValueError("{} algorithm requires sender_key but passed sender_key value is None"
                                 .format(alg.name))
        else:
            if sender_key is not None:
                raise ValueError("{} algorithm does not use sender_key but passed sender_key value is not None"
                                 .format(alg.name))

    def _validate_private_headers(self, header, alg):
        # only validate private headers when developers set
        # private headers explicitly
        if self._private_headers is None:
            return

        names = self.REGISTERED_HEADER_PARAMETER_NAMES.copy()
        names = names.union(self._private_headers)

        if alg.EXTRA_HEADERS:
            names = names.union(alg.EXTRA_HEADERS)

        for k in header:
            if k not in names:
                raise InvalidHeaderParameterNameError(k)


def prepare_key(alg, header, key):
    if callable(key):
        key = key(header, None)
    elif 'jwk' in header:
        key = header['jwk']
    return alg.prepare_key(key)
