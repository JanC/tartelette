import sys, os, logging
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from host.runner import LogfmtFormatter


def make_record(msg: str, level=logging.INFO, runner="r1", run_id="abc123") -> logging.LogRecord:
    record = logging.LogRecord(
        name="test", level=level, pathname="", lineno=0,
        msg=msg, args=(), exc_info=None,
    )
    record.runner = runner
    record.run_id = run_id
    return record


def test_logfmt_contains_required_fields():
    fmt = LogfmtFormatter()
    line = fmt.format(make_record("VM booted"))
    assert "ts=" in line
    assert "level=info" in line
    assert "runner=r1" in line
    assert "run_id=abc123" in line
    assert 'msg="VM booted"' in line


def test_logfmt_warns_level():
    fmt = LogfmtFormatter()
    line = fmt.format(make_record("shutting down", level=logging.WARNING))
    assert "level=warn" in line


def test_logfmt_error_level():
    fmt = LogfmtFormatter()
    line = fmt.format(make_record("boom", level=logging.ERROR))
    assert "level=error" in line


def test_logfmt_msg_without_spaces_no_quotes():
    fmt = LogfmtFormatter()
    line = fmt.format(make_record("booted"))
    assert "msg=booted" in line


def test_logfmt_msg_with_spaces_quoted():
    fmt = LogfmtFormatter()
    line = fmt.format(make_record("VM is ready"))
    assert 'msg="VM is ready"' in line


def test_logfmt_no_run_id_omitted():
    fmt = LogfmtFormatter()
    record = make_record("loop start", run_id="")
    line = fmt.format(record)
    assert "run_id=" not in line


def test_logfmt_ts_format():
    fmt = LogfmtFormatter()
    line = fmt.format(make_record("x"))
    # ts=2026-06-17T10:23:01Z
    import re
    assert re.search(r"ts=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", line)
