from authlib.common.errors import AuthlibBaseError


class JoseError(AuthlibBaseError):
    pass


class DecodeError(JoseError):
    error = 'decode_error'


class MissingAlgorithmError(JoseError):
    error = 'missing_algorithm'


class UnsupportedAlgorithmError(JoseError):
    error = 'unsupported_algorithm'


class BadSignatureError(JoseError):
    error = 'bad_signature'

    def __init__(self, result):
        super(BadSignatureError, self).__init__()
        self.result = result


class InvalidHeaderParameterNameError(JoseError):
    error = 'invalid_header_parameter_name'

    def __init__(self, name):
        description = 'Invalid Header Parameter Names: {}'.format(name)
        super(InvalidHeaderParameterNameError, self).__init__(
            description=description)


class InvalidEncryptionAlgorithmForECDH1PUWithKeyWrappingError(JoseError):
    error = 'invalid_encryption_algorithm_for_ECDH_1PU_with_key_wrapping'

    def __init__(self):
        description = 'In key agreement with key wrapping mode ECDH-1PU algorithm ' \
                      'only supports AES_CBC_HMAC_SHA2 family encryption algorithms'
        super(InvalidEncryptionAlgorithmForECDH1PUWithKeyWrappingError, self).__init__(
            description=description)


class MissingEncryptionAlgorithmError(JoseError):
    error = 'missing_encryption_algorithm'
    description = 'Missing "enc" in header'


class UnsupportedEncryptionAlgorithmError(JoseError):
    error = 'unsupported_encryption_algorithm'
    description = 'Unsupported "enc" value in header'


class UnsupportedCompressionAlgorithmError(JoseError):
    error = 'unsupported_compression_algorithm'
    description = 'Unsupported "zip" value in header'


class InvalidUseError(JoseError):
    error = 'invalid_use'
    description = 'Key "use" is not valid for your usage'


class InvalidClaimError(JoseError):
    error = 'invalid_claim'

    def __init__(self, claim):
        description = 'Invalid claim "{}"'.format(claim)
        super(InvalidClaimError, self).__init__(description=description)


class MissingClaimError(JoseError):
    error = 'missing_claim'

    def __init__(self, claim):
        description = 'Missing "{}" claim'.format(claim)
        super(MissingClaimError, self).__init__(description=description)


class InsecureClaimError(JoseError):
    error = 'insecure_claim'

    def __init__(self, claim):
        description = 'Insecure claim "{}"'.format(claim)
        super(InsecureClaimError, self).__init__(description=description)


class ExpiredTokenError(JoseError):
    error = 'expired_token'
    description = 'The token is expired'


class InvalidTokenError(JoseError):
    error = 'invalid_token'
    description = 'The token is not valid yet'
