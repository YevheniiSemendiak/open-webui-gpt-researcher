import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_values_schema_accepts_helm_inherited_globals() -> None:
    schema = json.loads((ROOT / "chart/open-webui-gpt-researcher/values.schema.json").read_text())

    assert schema["properties"]["global"] == {"type": "object"}


def test_migration_annotations_are_configurable_and_safe_by_default() -> None:
    migration = (ROOT / "chart/open-webui-gpt-researcher/templates/migrate.yaml").read_text()
    values = (ROOT / "chart/open-webui-gpt-researcher/values.yaml").read_text()

    assert ".Values.migration.annotations" in migration
    assert "helm.sh/hook: pre-install,pre-upgrade" in values
    assert "helm.sh/hook-weight:" in values
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
