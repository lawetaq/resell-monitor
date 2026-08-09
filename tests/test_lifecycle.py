import unittest

from src.sources.avito_transport import CurlCffiTransport, RequestsTransport
from src.sources.http import HttpTransport


class FakeSession:
    def __init__(self): self.closed = False
    def close(self): self.closed = True


class LifecycleTests(unittest.TestCase):
    def test_shared_http_transport_closes_session(self) -> None:
        session = FakeSession()
        transport = HttpTransport(session=session)  # type: ignore[arg-type]
        transport.close()
        self.assertTrue(session.closed)

    def test_requests_transport_closes_session(self) -> None:
        transport = RequestsTransport()
        session = FakeSession()
        transport.session = session  # type: ignore[assignment]
        transport.close()
        self.assertTrue(session.closed)

    def test_curl_transport_closes_lazily_created_session(self) -> None:
        transport = CurlCffiTransport()
        session = FakeSession()
        transport._session = session
        transport.close()
        self.assertTrue(session.closed)
        self.assertIsNone(transport._session)
