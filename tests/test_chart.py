from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_preinstall_migration_does_not_reference_release_service_account() -> None:
    migration = (ROOT / "chart/open-webui-gpt-researcher/templates/migrate.yaml").read_text()

    assert "helm.sh/hook: pre-install,pre-upgrade" in migration
    assert "serviceAccountName:" not in migration
    assert "automountServiceAccountToken: false" in migration
