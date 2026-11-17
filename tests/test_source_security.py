import socket

import pytest

from sctda import method_sources as ms


@pytest.mark.parametrize('address', ['127.0.0.1', 'ff02::1', '64:ff9b::7f00:1', '::ffff:127.0.0.1'])
def test_nonpublic_dns_answers_are_refused(monkeypatch, address):
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    monkeypatch.setattr(ms.socket, 'getaddrinfo', lambda *a, **k: [
        (family, socket.SOCK_STREAM, 6, '', (address, 443))])
    with pytest.raises(ms.SourceBlocked):
        ms._resolve_public_ips('satijalab.org')


def test_pinned_fetch_connects_to_checked_address(monkeypatch):
    used = []
    monkeypatch.setattr(ms, '_resolve_public_ips', lambda *a: ['8.8.8.8'])
    class Sock:
        pass
    class Context:
        def wrap_socket(self, sock, server_hostname):
            assert server_hostname == 'satijalab.org'
            return sock
    class Response:
        status = 200
        def getheader(self, name, default=None):
            return default
        def read(self, limit):
            return b'official content'
    class Connection:
        def __init__(self, *args, **kwargs):
            self.sock = None
        def request(self, method, target, headers):
            used.append((method, target))
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(ms.ssl, 'create_default_context', lambda: Context())
    monkeypatch.setattr(ms.socket, 'create_connection', lambda address, timeout: used.append(address) or Sock())
    monkeypatch.setattr(ms.http.client, 'HTTPSConnection', Connection)
    result = ms._open_url('https://satijalab.org/path', 5, 1024)
    assert result['body'] == b'official content'
    assert used[0] == ('8.8.8.8', 443)
