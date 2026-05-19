"""IPv4-only SMTP backend.

Railway's container runtime exposes an IPv6 interface but has no
working IPv6 egress route to Gmail's SMTP servers, so connecting to
`smtp.gmail.com`'s AAAA record fails with `[Errno 101] Network is
unreachable`. We pin resolution to A (IPv4) records before connecting.
TLS still verifies against the original hostname via SNI (smtplib uses
`self._host`, untouched), so cert validation works unchanged.
"""

import socket
import smtplib

from django.core.mail.backends.smtp import EmailBackend as DjangoSMTPEmailBackend


class _IPv4SMTP(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        last_err: OSError | None = None
        for af, socktype, proto, _canon, sa in infos:
            sock = None
            try:
                sock = socket.socket(af, socktype, proto)
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(timeout)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(sa)
                return sock
            except OSError as exc:
                last_err = exc
                if sock is not None:
                    sock.close()
        raise last_err or OSError("getaddrinfo returned no IPv4 results")


class IPv4EmailBackend(DjangoSMTPEmailBackend):
    @property
    def connection_class(self):
        return _IPv4SMTP
