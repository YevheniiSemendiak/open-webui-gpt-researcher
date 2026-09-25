import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_values_schema_accepts_helm_inherited_globals() -> None:
    schema = json.loads((ROOT / "chart/open-webui-gpt-researcher/values.schema.json").read_text())

    assert schema["properties"]["global"] == {"type": "object"}


def test_values_schema_documents_models_info_json() -> None:
    schema = json.loads((ROOT / "chart/open-webui-gpt-researcher/values.schema.json").read_text())

    models_info = schema["$defs"]["modelsInfo"]
    limits = schema["$defs"]["modelLimits"]
    api_env = schema["properties"]["api"]["properties"]["env"]

    assert models_info["additionalProperties"] == {"$ref": "#/$defs/modelLimits"}
    assert limits["required"] == ["context_length", "max_output_tokens"]
    assert limits["properties"]["context_length"]["minimum"] == 4096
    assert limits["properties"]["max_output_tokens"]["minimum"] == 1
    assert api_env["properties"]["MODELS_INFO"] == {"$ref": "#/$defs/modelsInfoJson"}


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


def test_chart_supports_value_from_environment() -> None:
    root = ROOT / "chart/open-webui-gpt-researcher"
    values = (root / "values.yaml").read_text()
    deployment = (root / "templates/deployments.yaml").read_text()
    migration = (root / "templates/migrate.yaml").read_text()
    function_sync = (root / "templates/function-sync.yaml").read_text()
    cleanup = (root / "templates/cleanup.yaml").read_text()

    assert values.count("extraEnv: []") == 6
    assert "RUNNER_EXTRA_ENV" in deployment
    assert ".Values.api.extraEnv" in deployment
    assert ".Values.api.extraEnv" in migration
    assert ".Values.api.extraEnv" in function_sync
    assert ".Values.api.extraEnv" in cleanup
    assert ".Values.migration.extraEnv" in migration
    assert ".Values.functionSync.extraEnv" in function_sync
    assert ".Values.cleanup.extraEnv" in cleanup


def test_service_monitor_is_opt_in() -> None:
    root = ROOT / "chart/open-webui-gpt-researcher"
    values = (root / "values.yaml").read_text()
    service_monitor = (root / "templates/servicemonitor.yaml").read_text()

    assert "serviceMonitor:\n    enabled: false" in values
    assert ".Values.api.serviceMonitor.enabled" in service_monitor
    assert "path: /metrics" in service_monitor
