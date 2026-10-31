"""Keep module-level ASGI application imports away from the user's live DB."""
import os
import tempfile

_prior_db = os.environ.get("XIANYU_DB_PATH")
_test_default = tempfile.TemporaryDirectory(prefix="xianyu-test-default-")
os.environ["XIANYU_DB_PATH"] = os.path.join(_test_default.name, "asgi-default.sqlite3")


def pytest_sessionfinish(session, exitstatus):
    if _prior_db is None:
        os.environ.pop("XIANYU_DB_PATH", None)
    else:
        os.environ["XIANYU_DB_PATH"] = _prior_db
    _test_default.cleanup()
