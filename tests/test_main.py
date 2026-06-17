import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from unittest.mock import patch
from host.runner import parse_args


def test_parse_args_defaults():
    args = parse_args([])
    assert args.parallel == 1
    assert args.env == ".env"
    assert args.log == "runner.log"


def test_parse_args_parallel():
    args = parse_args(["--parallel", "2"])
    assert args.parallel == 2


def test_parse_args_parallel_zero_rejected():
    with pytest.raises(SystemExit):
        parse_args(["--parallel", "0"])


def test_parse_args_parallel_negative_rejected():
    with pytest.raises(SystemExit):
        parse_args(["--parallel", "-1"])


def test_parse_args_custom_env():
    args = parse_args(["--env", "/tmp/custom.env"])
    assert args.env == "/tmp/custom.env"
