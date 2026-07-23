"""TLS compatibility helpers for older Redfish/BMC implementations."""

from __future__ import annotations

import ssl

from requests.adapters import HTTPAdapter


# Older BMCs commonly support TLS 1.2 but only offer static-RSA cipher suites.
# Python's secure default cipher list no longer advertises those suites, so add
# the AES/SHA-2 variants without enabling TLS 1.0, SSLv3, 3DES, or RC4.
_LEGACY_RSA_CIPHERS = (
    "AES256-GCM-SHA384",
    "AES128-GCM-SHA256",
    "AES256-SHA256",
    "AES128-SHA256",
)


def create_compatible_ssl_context() -> ssl.SSLContext:
    """Keep Python's modern defaults and add common older-BMC RSA suites."""
    context = ssl.create_default_context()
    default_ciphers = [
        cipher["name"]
        for cipher in context.get_ciphers()
        if cipher["protocol"] != "TLSv1.3"
    ]
    context.set_ciphers(":".join([*default_ciphers, *_LEGACY_RSA_CIPHERS]))

    # python-redfish-library controls certificate verification per request.
    # Its default is verify=False, which requires check_hostname to be disabled
    # before requests/urllib3 can set CERT_NONE on a supplied SSLContext.
    context.check_hostname = False
    return context


class CompatibleHTTPSAdapter(HTTPAdapter):
    """Requests adapter whose TLS offer also works with older TLS 1.2 BMCs."""

    def init_poolmanager(
        self,
        connections,
        maxsize,
        block=False,
        **pool_kwargs,
    ):
        pool_kwargs["ssl_context"] = create_compatible_ssl_context()
        return super().init_poolmanager(
            connections,
            maxsize,
            block=block,
            **pool_kwargs,
        )
