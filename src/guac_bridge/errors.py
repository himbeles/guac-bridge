"""Errors safe to display to the user without exposing connection secrets."""


class BridgeError(Exception):
    pass


class AuthenticationError(BridgeError):
    pass


class CredentialStoreUnavailable(BridgeError):
    pass
