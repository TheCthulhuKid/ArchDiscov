from concurrent.futures import ThreadPoolExecutor
from threading import Event, RLock

from archdiscov.ingestion.async_collection import PreparedSession


def test_session_lock_covers_creation_without_locking_returned_client_calls():
    creating = Event()
    release = Event()
    contended = Event()
    calls = []

    class ObservedLock:
        def __init__(self):
            self.lock = RLock()

        def __enter__(self):
            if creating.is_set():
                contended.set()
            self.lock.acquire()

        def __exit__(self, *args):
            self.lock.release()

    class Session:
        def client(self, name):
            calls.append(name)
            if name == "first":
                creating.set()
                assert release.wait(5)
            return name

    prepared = PreparedSession(Session())
    prepared._lock = ObservedLock()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(prepared.client, "first")
        try:
            assert creating.wait(5)
            second = pool.submit(lambda: prepared.client("second"))
            assert contended.wait(5)
            assert calls == ["first"]
            assert not second.done()
        finally:
            release.set()
        assert first.result(5) == "first"
        assert second.result(5) == "second"
    assert calls == ["first", "second"]
