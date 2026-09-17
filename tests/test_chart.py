from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_preinstall_migration_does_not_reference_release_service_account() -> None:
    migration = (ROOT / "chart/open-webui-gpt-researcher/templates/migrate.yaml").read_text()

    assert "helm.sh/hook: pre-install,pre-upgrade" in migration
    assert "serviceAccountName:" not in migration
    assert "automountServiceAccountToken: false" in migration


def test_application_images_default_to_the_chart_app_version() -> None:
    chart = (ROOT / "chart/open-webui-gpt-researcher/Chart.yaml").read_text()
    values = (ROOT / "chart/open-webui-gpt-researcher/values.yaml").read_text()
    templates = "\n".join(
        path.read_text()
        for path in (ROOT / "chart/open-webui-gpt-researcher/templates").glob("*.yaml")
    )

    assert 'appVersion: "0.0.0-dev"' in chart
    assert values.count('tag: ""') == 2
    assert templates.count(".Chart.AppVersion") == 5
