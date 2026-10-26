from datetime import datetime, timezone
from pathlib import Path

import pytest

from ssh_auto.discovery import discover
from ssh_auto.ssh_parser import parse_ssh_command, validate_options


def test_ssh_parser_boundaries_and_option_forms():
    parsed = parse_ssh_command("/usr/bin/ssh -vvv -p2222 -i ~/.ssh/id_ed25519 user@example.test")
    assert parsed is not None
    assert parsed.destination == "user@example.test"
    assert parsed.options == ("-vvv", "-p2222", "-i", str(Path.home() / ".ssh/id_ed25519"))
    assert validate_options("-J bastion -p 2222") == ("-J", "bastion", "-p", "2222")
    assert parse_ssh_command("ssh -o StrictHostKeyChecking=yes prod").options == ("-o", "StrictHostKeyChecking=yes")
    assert parse_ssh_command("ssh -oProxyJump=bastion prod").options == ("-oProxyJump=bastion",)
    for key in ("ProxyCommand", "localcommand", "KNOWNHOSTSCOMMAND", "RemoteCommand"):
        assert parse_ssh_command(f"ssh -o {key}=echo prod") is None
        assert parse_ssh_command(f"ssh -o{key}=echo prod") is None
    for value in ("ProxyCommand = echo", " rEmOtEcOmMaNd = id "):
        assert parse_ssh_command(f"ssh -o '{value}' prod") is None
        with pytest.raises(ValueError):
            validate_options(f"-o '{value}'")
    for option in ("-F /tmp/config", "-F/tmp/config"):
        assert parse_ssh_command(f"ssh {option} prod") is None
        with pytest.raises(ValueError):
            validate_options(option)
    assert validate_options("-J bastion -p 2222 -i ~/.ssh/id_ed25519")
    for command in (
        "ssh prod uname -a",
        "sshpass -p secret ssh prod",
        'ssh "$HOST"',
        "ssh -G prod",
        "ssh -Q cipher",
        "ssh -O check prod",
        "ssh prod | cat",
        "ssh prod $(id)",
    ):
        assert parse_ssh_command(command) is None, command

def test_discovery_merges_history_timestamps_and_config_includes(tmp_path: Path):
    home = tmp_path / "home"
    data = tmp_path / "data"
    (home / ".ssh" / "conf.d").mkdir(parents=True)
    (data / "fish").mkdir(parents=True)
    (home / ".bash_history").write_text("#1761467400\nssh -p 2222 alice@example.test\nssh safe \\\nssh intruder\nssh alice@example.test uname -a\n", encoding="utf-8")
    (home / ".zsh_history").write_text(": 1761467460:4;ssh -p 2222 alice@example.test\n: 1761467461:4;ssh safe \\\nssh intruder\n", encoding="utf-8")
    (data / "fish" / "fish_history").write_text("- cmd: ssh -p 2222 alice@example.test\n  when: 1761467520\n", encoding="utf-8")
    (home / ".ssh" / "config").write_text("Include conf.d/*\nInclude missing.conf\nInclude 'unterminated\nHost prod\nHost *.internal\n", encoding="utf-8")
    (home / ".ssh" / "conf.d" / "a.conf").write_text("Include conf.d/b.conf\nHost staging\n", encoding="utf-8")
    (home / ".ssh" / "conf.d" / "b.conf").write_text("Include conf.d/a.conf\n", encoding="utf-8")
    candidates, warnings = discover(
        {"HOME": str(home), "XDG_DATA_HOME": str(data), "SHELL": "/bin/bash"}, home
    )
    alice = next(item for item in candidates if item.destination == "alice@example.test")
    assert alice.options == ("-p", "2222")
    assert alice.use_count == 3
    assert alice.sources == frozenset({"bash", "zsh", "fish"})
    assert alice.last_seen_at == datetime.fromtimestamp(1761467520, timezone.utc)
    destinations = {item.destination for item in candidates}
    assert {"prod", "staging"} <= destinations
    assert "intruder" not in destinations and "*.internal" not in destinations
    assert any("missing.conf" in warning for warning in warnings)
    assert any("Ошибка" in warning for warning in warnings)


def test_custom_current_shell_history_file(tmp_path: Path):
    home = tmp_path / "home"
    custom = tmp_path / "custom.history"
    home.mkdir()
    custom.write_text("ssh custom.example\n", encoding="utf-8")
    candidates, _ = discover({"HOME": str(home), "SHELL": "/bin/bash", "HISTFILE": str(custom)}, home)
    assert candidates[0].destination == "custom.example"


def test_history_paths_fish_escapes_and_sort_order(tmp_path: Path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    data = tmp_path / "data"
    (data / "fish").mkdir(parents=True)
    (home / ".bash_history").write_text("#1761467400\nssh -i ~/.ssh/key z.example\nssh a.example\nssh a.example\n")
    (home / ".zsh_history").write_text(": 1761467401:0;ssh z.example\n")
    (data / "fish" / "fish_history").write_text("- cmd: ssh fish.example\\nssh intruder\n  when: 1761467402\n- cmd: ssh fish.example\n  when: 1761467403\n- cmd: ssh a.example\n")
    candidates, warnings = discover({"HOME": str(home), "XDG_DATA_HOME": str(data)}, home)
    assert warnings == []
    assert [(item.destination, item.use_count) for item in candidates] == [
        ("a.example", 3), ("fish.example", 1), ("z.example", 1), ("z.example", 1)
    ]
    assert [item.options for item in candidates[-2:]] == [(), ("-i", str(home / ".ssh/key"))]
    assert "intruder" not in {item.destination for item in candidates}
    assert next(item for item in candidates if item.options).options == ("-i", str(home / ".ssh/key"))
