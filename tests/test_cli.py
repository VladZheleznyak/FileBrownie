import pytest

from filebrownie.presentation.cli import main


def test_status_does_not_imply_processing(capsys):
    assert main(["status"]) == 0
    assert "No scan, extraction, history, or database integration" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["model-service", "model-setup"])
def test_model_placeholders_fail_explicitly(command, capsys):
    assert main([command]) == 2
    assert "SETUP_NOT_IMPLEMENTED" in capsys.readouterr().err
