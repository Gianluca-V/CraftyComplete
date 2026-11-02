import httpx
import pytest

from tests.conftest import CRAFTY, USER_TOKEN, make_client, make_logged_in_client

SERVER = "9b1968d1-0000-4000-8000-000000000003"
BACKUPS = f"/api/servers/{SERVER}/backups"
DEFAULT = "aaaa1111-0000-4000-8000-000000000001"
NIGHTLY = "bbbb2222-0000-4000-8000-000000000002"
ROOT = f"/var/opt/minecraft/crafty/crafty-4/backups/{SERVER}"


def config(backup_id, name, default=False, shutdown=False, backup_type="zip_vault"):
    return {"backup_id": backup_id, "backup_name": name, "backup_location": ROOT,
            "excluded_dirs": "", "max_backups": 0, "server_id": SERVER, "compress": False,
            "shutdown": shutdown, "before": "", "after": "", "default": default,
            "enabled": True, "backup_type": backup_type}


class FakeCrafty:
    """Fake Crafty backup API: configs, archive listings and the backup action."""

    def __init__(self, configs=None, archives=None):
        self.configs = configs if configs is not None else {
            DEFAULT: config(DEFAULT, "Default Backup", default=True)}
        self.archives = archives if archives is not None else {DEFAULT: [
            f"{ROOT}/{DEFAULT}/2026-10-30_14-05-09.zip",
            f"{ROOT}/{DEFAULT}/2026-11-01_09-00-00.zip",
        ]}
        self.backup_forbidden = False
        self.commands_forbidden = False
        # Crafty before 4.10.5 (e.g. 4.7.0) has no GET on the backup files route.
        self.lists_archives = True
        self.actions: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
        base = f"/api/v2/servers/{SERVER}"
        path = request.url.path
        denied = httpx.Response(400, json={"status": "error", "error": "NOT_AUTHORIZED"})
        if path == f"{base}/backups" and request.method == "GET":
            # Crafty answers this one without its {"status": "ok"} envelope.
            return denied if self.backup_forbidden else httpx.Response(200, json=self.configs)
        if (path.startswith(f"{base}/backups/backup/") and path.endswith("/files")
                and request.method == "GET"):
            if not self.lists_archives:
                return httpx.Response(405, json={"status": "error",
                                                 "error": "METHOD_NOT_ALLOWED"})
            if self.backup_forbidden:
                return denied
            backup_id = path.split("/")[-2]
            listed = self.archives.get(backup_id, [])
            return httpx.Response(200, json={"total": len(listed), "backups": listed})
        if path.startswith(f"{base}/action/backup_server/") and request.method == "POST":
            if self.commands_forbidden:
                return denied
            self.actions.append(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected call {request.method} {path}")


@pytest.fixture
def crafty():
    return FakeCrafty()


def client_for(crafty):
    return make_logged_in_client(crafty, crafty_url=CRAFTY, update_check_hours=0)


# --- listing ---------------------------------------------------------------------

def test_lists_backups_newest_first_with_their_date(crafty):
    with client_for(crafty) as client:
        response = client.get(BACKUPS)

    assert response.status_code == 200
    assert response.json() == {"archives_listed": True, "configs": [{
        "backup_id": DEFAULT, "name": "Default Backup", "default": True,
        "type": "zip_vault", "stops_server": False,
        "backups": [
            {"file": "2026-11-01_09-00-00.zip", "created": "2026-11-01T09:00:00"},
            {"file": "2026-10-30_14-05-09.zip", "created": "2026-10-30T14:05:09"},
        ],
    }]}


def test_crafty_without_the_backup_files_api_still_lists_configs_and_backs_up(crafty):
    crafty.lists_archives = False
    with client_for(crafty) as client:
        listed = client.get(BACKUPS)
        started = client.post(BACKUPS, json={})

    assert listed.status_code == 200
    assert listed.json() == {"archives_listed": False, "configs": [{
        "backup_id": DEFAULT, "name": "Default Backup", "default": True,
        "type": "zip_vault", "stops_server": False, "backups": None}]}
    assert started.status_code == 200
    assert crafty.actions == [DEFAULT]


def test_default_config_comes_first_and_odd_names_have_no_date():
    crafty = FakeCrafty(
        configs={NIGHTLY: config(NIGHTLY, "Nightly", shutdown=True),
                 DEFAULT: config(DEFAULT, "Default Backup", default=True)},
        archives={NIGHTLY: [f"{ROOT}/{NIGHTLY}/manual-copy.zip"]})
    with client_for(crafty) as client:
        configs = client.get(BACKUPS).json()["configs"]

    assert [(c["name"], c["stops_server"]) for c in configs] == [
        ("Default Backup", False), ("Nightly", True)]
    assert configs[0]["backups"] == []
    assert configs[1]["backups"] == [{"file": "manual-copy.zip", "created": None}]


def test_without_crafty_backup_permission_listing_is_403(crafty):
    crafty.backup_forbidden = True
    with client_for(crafty) as client:
        response = client.get(BACKUPS)

    assert response.status_code == 403
    assert response.json()["detail"] == "You do not have backup access to this server in Crafty."


# --- starting a backup -----------------------------------------------------------

def test_backup_now_uses_the_default_config(crafty):
    crafty.configs[NIGHTLY] = config(NIGHTLY, "Nightly")
    with client_for(crafty) as client:
        response = client.post(BACKUPS, json={})

    assert response.status_code == 200
    assert response.json() == {"backup_id": DEFAULT, "name": "Default Backup",
                               "stops_server": False, "started": True}
    assert crafty.actions == [DEFAULT]


def test_backup_now_with_a_chosen_config(crafty):
    crafty.configs[NIGHTLY] = config(NIGHTLY, "Nightly", shutdown=True)
    with client_for(crafty) as client:
        response = client.post(BACKUPS, json={"backup_id": NIGHTLY})

    assert response.json()["stops_server"] is True
    assert crafty.actions == [NIGHTLY]


def test_backup_now_with_an_unknown_config_is_404(crafty):
    with client_for(crafty) as client:
        response = client.post(BACKUPS, json={"backup_id": NIGHTLY})

    assert response.status_code == 404
    assert response.json()["detail"] == "This server has no backup configuration with that id."
    assert crafty.actions == []


def test_backup_now_without_any_config_is_409():
    crafty = FakeCrafty(configs={}, archives={})
    with client_for(crafty) as client:
        response = client.post(BACKUPS, json={})

    assert response.status_code == 409
    assert response.json()["detail"] == ("This server has no backup configuration in Crafty; "
                                         "create one in the Crafty panel.")


def test_backup_now_without_a_default_uses_the_only_config():
    crafty = FakeCrafty(configs={NIGHTLY: config(NIGHTLY, "Nightly")}, archives={})
    with client_for(crafty) as client:
        response = client.post(BACKUPS, json={})

    assert response.status_code == 200
    assert crafty.actions == [NIGHTLY]


def test_backup_now_without_commands_permission_is_403(crafty):
    crafty.commands_forbidden = True
    with client_for(crafty) as client:
        response = client.post(BACKUPS, json={})

    assert response.status_code == 403
    assert response.json()["detail"] == ("You do not have permission to back up this server in "
                                         "Crafty (it needs the Commands permission).")


def test_backup_now_is_refused_while_a_modpack_update_runs(crafty):
    with client_for(crafty) as client:
        with client.app.state.locks.marked_busy(SERVER, "A modpack update is running."):
            response = client.post(BACKUPS, json={})

    assert response.status_code == 409
    assert crafty.actions == []


@pytest.mark.parametrize("bad", ["../x", "not a uuid", "A" * 65])
def test_backup_ids_are_checked(crafty, bad):
    with client_for(crafty) as client:
        response = client.post(BACKUPS, json={"backup_id": bad})

    assert response.status_code == 422
    assert crafty.actions == []


def test_backup_routes_need_a_session(crafty):
    with make_client(crafty, crafty_url=CRAFTY) as client:
        responses = [client.get(BACKUPS), client.post(BACKUPS, json={})]

    assert [r.status_code for r in responses] == [401, 401]
    assert crafty.actions == []


def test_server_page_has_the_backups_panel(crafty):
    with client_for(crafty) as client:
        page = client.get(f"/servers/{SERVER}").text

    assert 'id="backups-panel"' in page
    assert 'id="backup-now"' in page
    assert "server_backups.js" in page


# --- home page -------------------------------------------------------------------

def test_home_is_the_servers_list_and_browse_moved(crafty):
    with client_for(crafty) as client:
        home = client.get("/").text
        browse = client.get("/browse").text

    assert 'id="server-list"' in home
    assert 'href="/servers" aria-current="page"' in home
    assert 'id="search-input"' in browse
