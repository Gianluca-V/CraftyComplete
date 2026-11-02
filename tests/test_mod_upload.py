import pytest

from tests.test_adoption import FILES, SERVER, by_file, sha1
from tests.test_install import FakeServices, Session

UPLOAD = f"/api/servers/{SERVER}/mods/upload"
OWN_JAR = b"PK\x03\x04my-other-build"
JAR = {"Content-Type": "application/octet-stream"}


@pytest.fixture
def fakes():
    return FakeServices()


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "db.sqlite3")


def upload(session, name, data, headers=JAR):
    return session.client.post(UPLOAD, params={"file_name": name}, content=data,
                               headers=headers)


def test_uploading_a_jar_puts_it_in_mods_and_lists_it(fakes, db):
    s = Session(fakes, db)
    response = upload(s, "my-mod-1.0.jar", OWN_JAR)
    mods = s.mods()
    s.close()

    assert response.status_code == 200
    assert response.json() == {"file_name": "my-mod-1.0.jar", "title": None,
                               "state": "unknown", "warnings": [], "restart_required": True}
    assert fakes.files["mods/my-mod-1.0.jar"] == OWN_JAR
    assert mods["my-mod-1.0.jar"]["sha1"] == sha1(OWN_JAR)
    assert mods["my-mod-1.0.jar"]["dependency"] is False
    # The mods already there keep their identity.
    assert mods["fabric-api.jar"]["title"] == "Fabric API"


def test_an_uploaded_jar_is_identified_on_modrinth_and_client_only_is_warned(db):
    files = {k: v for k, v in FILES.items() if k != "mods/sodium.jar"}
    fakes = FakeServices(files=files)
    s = Session(fakes, db)
    response = upload(s, "sodium-fabric.jar", FILES["mods/sodium.jar"])
    s.close()

    assert response.status_code == 200
    assert response.json()["title"] == "Sodium"
    assert response.json()["state"] == "identified"
    assert response.json()["warnings"] == [
        "Sodium is a client-only mod on Modrinth; the server may not start with it."]


def test_uploading_a_second_jar_of_an_installed_mod_is_warned(fakes, db):
    s = Session(fakes, db)
    response = upload(s, "sodium-copy.jar", FILES["mods/sodium.jar"])
    s.close()

    assert response.status_code == 200
    assert ("sodium.jar is the same mod (Sodium); keep only one of them."
            in response.json()["warnings"])


def test_uploading_over_an_existing_file_is_refused(fakes, db):
    s = Session(fakes, db)
    response = upload(s, "custom.jar", OWN_JAR)
    s.close()

    assert response.status_code == 409
    assert response.json()["detail"] == ("mods/custom.jar already exists; remove it first or "
                                         "rename your file.")
    assert fakes.files["mods/custom.jar"] == b"my-own-build"


@pytest.mark.parametrize("name", ["readme.txt", "../evil.jar", "a/b.jar", "café.jar",
                                  ".hidden.jar", "x" * 252 + ".jar", ""])
def test_only_plain_jar_names_are_accepted(fakes, db, name):
    s = Session(fakes, db)
    response = upload(s, name, OWN_JAR)
    uploads = fakes.uploads()
    s.close()

    assert response.status_code == 422
    assert uploads == []


def test_empty_and_oversized_uploads_are_refused(fakes, db):
    s = Session(fakes, db)
    empty = upload(s, "empty.jar", b"")
    big = upload(s, "big.jar", b"x" * (90 * 1024 * 1024 + 1))
    uploads = fakes.uploads()
    s.close()

    assert empty.status_code == 422
    assert big.status_code == 413
    assert big.json()["detail"] == "big.jar is larger than 90 MiB; upload it in Crafty instead."
    assert uploads == []


def test_uploads_must_be_sent_as_binary(fakes, db):
    s = Session(fakes, db)
    response = upload(s, "my-mod.jar", OWN_JAR, headers={"Content-Type": "text/plain"})
    uploads = fakes.uploads()
    s.close()

    assert response.status_code == 415
    assert uploads == []


def test_uploading_needs_an_adopted_server(fakes, db):
    s = Session(fakes, db, adopt=False)
    response = upload(s, "my-mod.jar", OWN_JAR)
    s.close()

    assert response.status_code == 409
    assert response.json() == {"detail": "Adopt this server first."}


def test_uploading_is_refused_while_a_modpack_update_runs(fakes, db):
    s = Session(fakes, db)
    with s.client.app.state.locks.marked_busy(SERVER, "A modpack update is running."):
        response = upload(s, "my-mod.jar", OWN_JAR)
    uploads = fakes.uploads()
    s.close()

    assert response.status_code == 409
    assert uploads == []


def test_when_modrinth_is_down_the_jar_is_uploaded_and_a_rescan_is_suggested(fakes, db):
    s = Session(fakes, db)
    fakes.modrinth_down = True
    response = upload(s, "my-mod.jar", OWN_JAR)
    s.close()

    assert response.status_code == 200
    assert fakes.files["mods/my-mod.jar"] == OWN_JAR
    assert response.json()["warnings"] == [
        "my-mod.jar was uploaded, but Modrinth could not be reached to identify it; "
        "rescan the server to list it."]


def test_server_page_has_the_drop_zone_and_the_search_boxes(fakes, db):
    s = Session(fakes, db)
    page = s.client.get(f"/servers/{SERVER}").text
    s.close()

    assert 'id="mod-drop"' in page
    assert 'id="mod-search"' in page
    assert 'id="files-search"' in page
    assert "server_mod_upload.js" in page
