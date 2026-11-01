import json

import httpx
import pytest

from tests.test_adoption import SERVER
from tests.test_install import (BYTES, PROJECTS, VERSIONS_BY_ID, FakeServices, Session,
                                dep, install, version)

MODS = f"/api/servers/{SERVER}/mods"

# A second mod that requires Cloth Config, so Cloth Config is shared.
PROJECTS["PC"] = {"id": "PC", "slug": "shares-cloth", "title": "Shares Cloth",
                  "server_side": "required", "client_side": "required", "project_type": "mod",
                  "icon_url": None}
BYTES["sc-1"] = b"shares-cloth"
VERSIONS_BY_ID["sc-1"] = version("sc-1", "PC", "1.0", "shares-cloth-1.0.jar", BYTES["sc-1"],
                                 deps=[dep("P4")])


class Services(FakeServices):
    """Install fakes plus Modrinth's bulk version lookup and the extra project."""

    def _modrinth(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/versions":
            ids = json.loads(request.url.params["ids"])
            return httpx.Response(200, json=[VERSIONS_BY_ID[i] for i in ids
                                             if i in VERSIONS_BY_ID])
        parts = path.split("/")
        if len(parts) == 5 and parts[3] == "PC" and parts[4] == "version":
            return httpx.Response(200, json=[VERSIONS_BY_ID["sc-1"]])
        return super()._modrinth(request)


@pytest.fixture
def fakes():
    return Services()


@pytest.fixture
def session(fakes, tmp_path):
    s = Session(fakes, str(tmp_path / "db.sqlite3"))
    yield s
    s.close()


def removal(session, file_name):
    return session.client.get(f"{MODS}/{file_name}/removal")


def remove(session, file_name, **body):
    return session.post(f"{MODS}/{file_name}/remove", body)


# --- removal plan ---------------------------------------------------------------

def test_removal_plan_lists_dependencies_nothing_else_needs(session):
    install(session, "P3")  # Lithium pulls in Cloth Config

    response = removal(session, "lithium-0.11.2.jar")

    assert response.status_code == 200
    assert response.json() == {
        "file_name": "lithium-0.11.2.jar",
        "title": "Lithium",
        "required_by": [],
        "unused_dependencies": [{"file_name": "cloth-config-11.1.jar", "title": "Cloth Config"}],
        "warnings": [],
    }


def test_removal_plan_names_mods_that_require_it(session):
    install(session, "P3")

    body = removal(session, "cloth-config-11.1.jar").json()

    assert body["required_by"] == [{"file_name": "lithium-0.11.2.jar", "title": "Lithium"}]
    assert body["unused_dependencies"] == []


def test_dependency_still_needed_by_another_mod_is_not_offered(session):
    install(session, "P3")
    install(session, "PC")  # also requires Cloth Config

    body = removal(session, "lithium-0.11.2.jar").json()

    assert body["unused_dependencies"] == []


def test_removal_plan_of_an_unknown_mod(session):
    response = removal(session, "nope.jar")

    assert response.status_code == 404
    assert response.json() == {"detail": "No adopted mod with that file name."}


# --- removing -------------------------------------------------------------------

def test_remove_deletes_only_the_mod_when_no_dependency_is_chosen(session, fakes):
    install(session, "P3")

    response = remove(session, "lithium-0.11.2.jar")

    assert response.status_code == 200
    body = response.json()
    assert body["removed"] == [{"file_name": "lithium-0.11.2.jar", "title": "Lithium"}]
    assert body["restart_required"] is True
    assert "mods/lithium-0.11.2.jar" not in fakes.files
    assert "mods/cloth-config-11.1.jar" in fakes.files
    assert set(session.mods()) == {"fabric-api.jar", "sodium.jar", "custom.jar",
                                   "cloth-config-11.1.jar"}


def test_remove_also_deletes_confirmed_unused_dependencies(session, fakes):
    install(session, "P3")

    body = remove(session, "lithium-0.11.2.jar",
                  remove_dependencies=["cloth-config-11.1.jar"]).json()

    assert [m["file_name"] for m in body["removed"]] == ["lithium-0.11.2.jar",
                                                        "cloth-config-11.1.jar"]
    assert "mods/cloth-config-11.1.jar" not in fakes.files
    assert "cloth-config-11.1.jar" not in session.mods()


def test_removing_a_required_mod_needs_confirmation(session, fakes):
    install(session, "P3")

    refused = remove(session, "cloth-config-11.1.jar")
    forced = remove(session, "cloth-config-11.1.jar", force=True)

    assert refused.status_code == 409
    assert refused.json() == {"detail": "Cloth Config is required by Lithium. "
                                        "Confirm to remove it anyway."}
    assert forced.status_code == 200
    assert "mods/cloth-config-11.1.jar" not in fakes.files


def test_dependency_not_in_the_plan_is_refused_and_nothing_is_deleted(session, fakes):
    install(session, "P3")
    install(session, "PC")

    response = remove(session, "lithium-0.11.2.jar",
                      remove_dependencies=["cloth-config-11.1.jar"])

    assert response.status_code == 409
    assert response.json() == {"detail": "cloth-config-11.1.jar is not an unused dependency "
                                         "of Lithium; review the removal again."}
    assert "mods/lithium-0.11.2.jar" in fakes.files
    assert fakes.deletes() == []


def test_removing_a_modpack_file_warns_that_an_update_brings_it_back(session, fakes):
    install(session, "P3")
    session.client.app.state.adoption_store.set_modpack_files(
        SERVER, {"mods/lithium-0.11.2.jar": "x"})

    plan = removal(session, "lithium-0.11.2.jar").json()
    body = remove(session, "lithium-0.11.2.jar").json()

    warning = "Lithium is part of the linked modpack; a modpack update adds it back."
    assert plan["warnings"] == [warning]
    assert body["warnings"] == [warning]


def test_failed_delete_reports_what_was_already_removed(session, fakes):
    install(session, "P3")
    fakes.delete_error = (500, "boom")

    response = remove(session, "lithium-0.11.2.jar")

    assert response.status_code == 502
    assert response.json() == {"detail": "Crafty could not delete lithium-0.11.2.jar (boom)."}
    assert "lithium-0.11.2.jar" in session.mods()


def test_remove_needs_a_json_body(session):
    response = session.client.post(f"{MODS}/custom.jar/remove")

    assert response.status_code == 422


def test_remove_is_refused_during_a_modpack_update(session, fakes):
    with session.client.app.state.locks.marked_busy(SERVER, "busy"):
        response = remove(session, "custom.jar")

    assert response.status_code == 409
    assert "mods/custom.jar" in fakes.files


# --- picking a version ------------------------------------------------------------

def test_versions_lists_compatible_versions_newest_first(session):
    install(session, "P3")

    response = session.client.get(f"{MODS}/lithium-0.11.2.jar/versions")

    assert response.status_code == 200
    assert response.json() == {
        "current_version_id": "li-2",
        "versions": [
            {"id": "li-2", "version_number": "0.11.2", "version_type": "release",
             "date_published": None},
            {"id": "li-3b", "version_number": "0.12.0-beta", "version_type": "beta",
             "date_published": None},
            {"id": "li-1", "version_number": "0.11.1", "version_type": "release",
             "date_published": None},
        ],
    }


def test_picking_an_older_version_replaces_the_jar_without_pinning(session, fakes):
    install(session, "P3")

    response = session.post(f"{MODS}/lithium-0.11.2.jar/version", {"version_id": "li-1"})

    assert response.status_code == 200
    body = response.json()
    assert body["updated"] == [{
        "project_id": "P3", "title": "Lithium", "from_version": "0.11.2",
        "to_version": "0.11.1", "old_file_name": "lithium-0.11.2.jar",
        "file_name": "lithium-0.11.1.jar"}]
    assert fakes.files["mods/lithium-0.11.1.jar"] == BYTES["li-1"]
    assert "mods/lithium-0.11.2.jar" not in fakes.files
    mod = session.mods()["lithium-0.11.1.jar"]
    assert mod["pinned"] is False
    # "Update all" can bring it back to the newest release.
    assert mod["update_available"] is True
    assert mod["latest_version_id"] == "li-2"


def test_picking_an_incompatible_or_foreign_version_is_refused(session, fakes):
    install(session, "P3")

    response = session.post(f"{MODS}/lithium-0.11.2.jar/version", {"version_id": "cl-1"})

    assert response.status_code == 422
    assert response.json() == {"detail": "Lithium has no version cl-1 for fabric 1.20.1."}
    assert "mods/lithium-0.11.2.jar" in fakes.files


def test_picking_the_installed_version_is_refused(session):
    install(session, "P3")

    response = session.post(f"{MODS}/lithium-0.11.2.jar/version", {"version_id": "li-2"})

    assert response.status_code == 409
    assert response.json() == {"detail": "Lithium 0.11.2 is already installed."}


def test_picking_a_version_of_a_pinned_mod_is_refused(session):
    install(session, "P3")
    session.client.patch(f"{MODS}/lithium-0.11.2.jar", json={"pinned": True})

    response = session.post(f"{MODS}/lithium-0.11.2.jar/version", {"version_id": "li-1"})

    assert response.status_code == 409
    assert response.json() == {"detail": "Lithium is pinned; unpin it to change its version."}


def test_versions_of_a_mod_not_on_modrinth_are_refused(session):
    response = session.client.get(f"{MODS}/custom.jar/versions")

    assert response.status_code == 409
    assert response.json() == {
        "detail": "custom.jar is not on Modrinth, so its version cannot be changed."}


def test_update_still_moves_a_mod_to_the_newest_version(session, fakes):
    """The existing single-mod update keeps working after the shared refactor."""
    install(session, "P3")
    session.post(f"{MODS}/lithium-0.11.2.jar/version", {"version_id": "li-1"})

    response = session.post(f"{MODS}/lithium-0.11.1.jar/update")

    assert response.status_code == 200
    assert response.json()["updated"][0]["to_version"] == "0.11.2"
    assert fakes.files["mods/lithium-0.11.2.jar"] == BYTES["li-2"]



# --- which mods count as installed dependencies --------------------------------------

def test_mods_the_user_added_are_never_offered_as_unused_dependencies(session):
    """Fabric API was adopted, not installed as a dependency, so it stays out of the plan."""
    install(session, "P3")

    mods = session.mods()
    plan = removal(session, "lithium-0.11.2.jar").json()

    assert mods["cloth-config-11.1.jar"]["dependency"] is True
    assert mods["lithium-0.11.2.jar"]["dependency"] is False
    assert mods["fabric-api.jar"]["dependency"] is False
    assert "fabric-api.jar" not in [m["file_name"] for m in plan["unused_dependencies"]]


def test_rescan_keeps_which_mods_were_installed_as_dependencies(session):
    install(session, "P3")

    session.post(f"/api/servers/{SERVER}/scan", {})

    assert session.mods()["cloth-config-11.1.jar"]["dependency"] is True


def test_updating_a_dependency_keeps_it_marked(session):
    install(session, "P3")
    session.post(f"{MODS}/lithium-0.11.2.jar/version", {"version_id": "li-1"})

    assert session.mods()["lithium-0.11.1.jar"]["dependency"] is False
    assert session.mods()["cloth-config-11.1.jar"]["dependency"] is True
