{{- define "research.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "research.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "research.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "research.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
app.kubernetes.io/name: {{ include "research.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "research.selectorLabels" -}}
app.kubernetes.io/name: {{ include "research.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "research.controllerServiceAccount" -}}
{{- default (printf "%s-controller" (include "research.fullname" .)) .Values.serviceAccount.controllerName }}
{{- end }}

{{- define "research.runnerServiceAccount" -}}
{{- default (printf "%s-runner" (include "research.fullname" .)) .Values.serviceAccount.runnerName }}
{{- end }}

{{- define "research.secretName" -}}
{{- default (include "research.fullname" .) .Values.secrets.existingSecret }}
{{- end }}

{{- define "research.image" -}}
{{ printf "%s:%s" .Values.image.repository .Values.image.tag }}
{{- end }}

{{- define "research.commonEnv" -}}
- name: RESEARCH_ENVIRONMENT
  value: {{ .Values.config.environment | quote }}
- name: RESEARCH_DATABASE_URL
  value: {{ .Values.config.databaseUrl | quote }}
- name: RESEARCH_ARTIFACT_BASE_URL
  value: {{ .Values.config.artifactBaseUrl | quote }}
- name: RESEARCH_INTERNAL_BASE_URL
  value: {{ printf "http://%s:%v" (include "research.fullname" .) .Values.service.port | quote }}
- name: RESEARCH_OPENWEBUI_URL
  value: {{ .Values.config.openwebuiUrl | quote }}
- name: RESEARCH_ARTIFACT_BACKEND
  value: {{ .Values.config.artifactBackend | quote }}
- name: RESEARCH_ARTIFACT_PATH
  value: {{ .Values.config.artifactPath | quote }}
- name: RESEARCH_S3_ENDPOINT_URL
  value: {{ .Values.config.s3EndpointUrl | quote }}
- name: RESEARCH_S3_REGION
  value: {{ .Values.config.s3Region | quote }}
- name: RESEARCH_S3_BUCKET
  value: {{ .Values.config.s3Bucket | quote }}
- name: RESEARCH_EXECUTOR
  value: {{ .Values.config.executor | quote }}
- name: RESEARCH_ENGINE
  value: {{ .Values.config.engine | quote }}
- name: RESEARCH_PUBLIC_SEARCH_ENABLED
  value: {{ .Values.config.publicSearchEnabled | quote }}
- name: RESEARCH_MODEL_ROUTE
  value: {{ .Values.config.modelRoute | quote }}
- name: RESEARCH_MODEL_PROFILES
  value: {{ .Values.config.modelProfiles | toJson | quote }}
- name: RESEARCH_EMBEDDING_MODEL
  value: {{ .Values.config.embeddingModel | quote }}
- name: RESEARCH_MAX_CONCURRENT_JOBS
  value: {{ .Values.config.maxConcurrentJobs | quote }}
- name: RESEARCH_CONTROLLER_LEADER_ELECTION
  value: {{ .Values.config.controllerLeaderElection | quote }}
- name: RESEARCH_CONTROLLER_LEADER_LOCK_ID
  value: {{ .Values.config.controllerLeaderLockId | quote }}
- name: RESEARCH_CONTROLLER_LEADER_RETRY_SECONDS
  value: {{ .Values.config.controllerLeaderRetrySeconds | quote }}
- name: RESEARCH_HARD_MAX_INPUT_TOKENS
  value: {{ .Values.config.hardMaxInputTokens | quote }}
- name: RESEARCH_HARD_MAX_OUTPUT_TOKENS
  value: {{ .Values.config.hardMaxOutputTokens | quote }}
- name: RESEARCH_HARD_MAX_SEARCHES
  value: {{ .Values.config.hardMaxSearches | quote }}
- name: RESEARCH_HARD_MAX_WALL_TIME_SECONDS
  value: {{ .Values.config.hardMaxWallTimeSeconds | quote }}
- name: RESEARCH_RUNNER_IMAGE
  value: {{ include "research.image" . | quote }}
- name: RESEARCH_RUNNER_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
- name: RESEARCH_RUNNER_OWNER_DEPLOYMENT
  value: {{ printf "%s-controller" (include "research.fullname" .) | quote }}
- name: RESEARCH_RUNNER_SERVICE_ACCOUNT
  value: {{ include "research.runnerServiceAccount" . | quote }}
- name: RESEARCH_RUNNER_ACTIVE_DEADLINE_SECONDS
  value: {{ .Values.config.runner.activeDeadlineSeconds | quote }}
- name: RESEARCH_RUNNER_TTL_SECONDS_AFTER_FINISHED
  value: {{ .Values.config.runner.ttlSecondsAfterFinished | quote }}
- name: RESEARCH_RUNNER_CPU_REQUEST
  value: {{ .Values.config.runner.resources.requests.cpu | quote }}
- name: RESEARCH_RUNNER_MEMORY_REQUEST
  value: {{ .Values.config.runner.resources.requests.memory | quote }}
- name: RESEARCH_RUNNER_CPU_LIMIT
  value: {{ .Values.config.runner.resources.limits.cpu | quote }}
- name: RESEARCH_RUNNER_MEMORY_LIMIT
  value: {{ .Values.config.runner.resources.limits.memory | quote }}
{{- if .Values.config.runner.extraEnvSecret }}
- name: RESEARCH_RUNNER_EXTRA_ENV_SECRET
  value: {{ .Values.config.runner.extraEnvSecret | quote }}
{{- end }}
{{- end }}

{{- define "research.apiSecretEnv" -}}
- name: RESEARCH_SERVICE_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "research.secretName" . }}
      key: service-token
- name: RESEARCH_SIGNING_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "research.secretName" . }}
      key: signing-secret
- name: RESEARCH_OPENWEBUI_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "research.secretName" . }}
      key: openwebui-api-key
- name: RESEARCH_S3_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ include "research.secretName" . }}
      key: s3-access-key-id
- name: RESEARCH_S3_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "research.secretName" . }}
      key: s3-secret-access-key
{{- end }}
