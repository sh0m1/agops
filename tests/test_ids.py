from agops.ids import normalize_remote, project_id_from_remote


def test_git_remote_forms_have_one_project_identity() -> None:
    https = "https://github.com/acme/widgets.git"
    ssh = "git@github.com:acme/widgets.git"
    assert normalize_remote(https) == normalize_remote(ssh) == "github.com/acme/widgets"
    assert project_id_from_remote(https) == "acme-widgets"


def test_remote_credentials_are_not_part_of_identity() -> None:
    remote = "https://secret-token@github.com/acme/widgets.git"
    assert normalize_remote(remote) == "github.com/acme/widgets"
