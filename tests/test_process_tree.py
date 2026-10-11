import datetime

from common import process_tree


def test_parse_optimizer_owner_rejects_invalid_names():
    assert process_tree.parse_optimizer_owner("stable-study") is None
    parsed = process_tree.parse_optimizer_owner("optimizer_RUN20261011-120000_4321")
    assert parsed["pid"] == 4321
    assert parsed["owner_time"] == datetime.datetime(2026, 10, 11, 12, 0, 0).timestamp()


def test_optimizer_owner_process_requires_matching_process_identity(monkeypatch):
    owner = "optimizer_RUN20261011-120000_4321"
    monkeypatch.setattr(
        process_tree,
        "_process_table",
        lambda: {
            4321: {
                "pid": 4321,
                "ppid": 1,
                "identity": 1,
                "started": datetime.datetime(2026, 10, 11, 11, 59, 59).timestamp(),
                "command": "python /srv/QuantAda/run.py strategy --opt_params {}",
            }
        },
    )

    result = process_tree.optimizer_owner_process(owner)

    assert result["pid"] == 4321


def test_optimizer_owner_process_rejects_pid_reuse_and_unrelated_command(monkeypatch):
    owner = "optimizer_RUN20261011-120000_4321"
    monkeypatch.setattr(
        process_tree,
        "_process_table",
        lambda: {
            4321: {
                "pid": 4321,
                "ppid": 1,
                "identity": 1,
                "started": datetime.datetime(2026, 10, 11, 12, 1, 0).timestamp(),
                "command": "python /srv/other_job.py",
            }
        },
    )

    assert process_tree.optimizer_owner_process(owner) is None


def test_optimizer_process_tree_returns_empty_when_owner_exits_during_scan(monkeypatch):
    owner = "optimizer_RUN20261011-120000_4321"
    root = {
        "pid": 4321,
        "ppid": 1,
        "identity": 1,
        "started": datetime.datetime(2026, 10, 11, 11, 59, 59).timestamp(),
        "command": "python /srv/QuantAda/run.py strategy",
    }
    calls = iter([{4321: root}, {}])
    monkeypatch.setattr(process_tree, "_process_table", lambda: next(calls))

    assert process_tree.optimizer_process_tree(owner) == []


def test_same_process_rejects_missing_identity(monkeypatch):
    monkeypatch.setattr(process_tree, "_process_table", lambda: {4321: {"identity": None}})

    assert process_tree._same_process({"pid": 4321, "identity": None}) is False
