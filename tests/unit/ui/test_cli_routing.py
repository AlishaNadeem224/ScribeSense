@pytest.mark.parametrize("command", ["reset", "recover", "uninstall", "doctor"])
def test_recovery_commands_do_not_load_ui(
    command: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    monkeypatch.setitem(sys.modules, "scribesense.ui.app", None)

    code = cli.main(["scribesense", command])
    assert code == int(ExitCode.FAILED)
    assert command in capsys.readouterr().err