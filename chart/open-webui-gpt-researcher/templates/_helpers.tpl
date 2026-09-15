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

{{- define "research.databaseSecretName" -}}
{{- default (include "research.secretName" .) .Values.database.existingSecret }}
{{- end }}

{{- define "research.image" -}}
{{ printf "%s:%s" .Values.image.repository .Values.image.tag }}
{{- end }}

{{- define "research.commonEnv" -}}
- name: ENVIRONMENT
  value: {{ .Values.config.environment | quote }}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "research.databaseSecretName" . }}
      key: {{ .Values.database.secretKey }}
- name: ARTIFACT_BASE_URL
  value: {{ .Values.config.artifactBaseUrl | quote }}
- name: INTERNAL_BASE_URL
  value: {{ printf "http://%s:%v" (include "research.fullname" .) .Values.service.port | quote }}
- name: OPENWEBUI_URL
  value: {{ .Values.config.openwebuiUrl | quote }}
- name: ARTIFACT_BACKEND
  value: {{ .Values.config.artifactBackend | quote }}
- name: ARTIFACT_PATH
  value: {{ .Values.config.artifactPath | quote }}
- name: S3_ENDPOINT_URL
  value: {{ .Values.config.s3EndpointUrl | quote }}
- name: S3_REGION
  value: {{ .Values.config.s3Region | quote }}
- name: S3_BUCKET
  value: {{ .Values.config.s3Bucket | quote }}
- name: MODE
  value: {{ .Values.config.mode | quote }}
- name: PUBLIC_SEARCH_ENABLED
  value: {{ .Values.config.publicSearchEnabled | quote }}
- name: MODEL_ROUTE
  value: {{ .Values.config.modelRoute | quote }}
- name: MODEL_PROFILES
  value: {{ .Values.config.modelProfiles | toJson | quote }}
- name: EMBEDDING_MODEL
  value: {{ .Values.config.embeddingModel | quote }}
- name: MAX_CONCURRENT_JOBS
  value: {{ .Values.config.maxConcurrentJobs | quote }}
- name: CONTROLLER_LEADER_LOCK_ID
  value: {{ .Values.config.controllerLeaderLockId | quote }}
- name: CONTROLLER_LEADER_RETRY_SECONDS
  value: {{ .Values.config.controllerLeaderRetrySeconds | quote }}
- name: DISPATCH_LEASE_SECONDS
  value: {{ .Values.config.dispatchLeaseSeconds | quote }}
- name: DISPATCH_RECONCILE_BATCH_SIZE
  value: {{ .Values.config.dispatchReconcileBatchSize | quote }}
- name: HARD_MAX_INPUT_TOKENS
  value: {{ .Values.config.hardMaxInputTokens | quote }}
- name: HARD_MAX_OUTPUT_TOKENS
  value: {{ .Values.config.hardMaxOutputTokens | quote }}
- name: HARD_MAX_SEARCHES
  value: {{ .Values.config.hardMaxSearches | quote }}
- name: HARD_MAX_WALL_TIME_SECONDS
  value: {{ .Values.config.hardMaxWallTimeSeconds | quote }}
- name: EVENT_RETENTION_DAYS
  value: {{ .Values.config.eventRetentionDays | quote }}
- name: ARTIFACT_RETENTION_DAYS
  value: {{ .Values.config.artifactRetentionDays | quote }}
- name: JOB_RETENTION_DAYS
  value: {{ .Values.config.jobRetentionDays | quote }}
- name: ORPHAN_GRACE_SECONDS
  value: {{ .Values.config.orphanGraceSeconds | quote }}
- name: CLEANUP_BATCH_SIZE
  value: {{ .Values.config.cleanupBatchSize | quote }}
- name: RUNNER_IMAGE
  value: {{ include "research.image" . | quote }}
- name: RUNNER_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
- name: RUNNER_OWNER_DEPLOYMENT
  value: {{ printf "%s-controller" (include "research.fullname" .) | quote }}
- name: RUNNER_SERVICE_ACCOUNT
  value: {{ include "research.runnerServiceAccount" . | quote }}
- name: RUNNER_ACTIVE_DEADLINE_SECONDS
  value: {{ .Values.config.runner.activeDeadlineSeconds | quote }}
- name: RUNNER_TTL_SECONDS_AFTER_FINISHED
  value: {{ .Values.config.runner.ttlSecondsAfterFinished | quote }}
- name: RUNNER_CPU_REQUEST
  value: {{ .Values.config.runner.resources.requests.cpu | quote }}
- name: RUNNER_MEMORY_REQUEST
  value: {{ .Values.config.runner.resources.requests.memory | quote }}
- name: RUNNER_CPU_LIMIT
  value: {{ .Values.config.runner.resources.limits.cpu | quote }}
- name: RUNNER_MEMORY_LIMIT
  value: {{ .Values.config.runner.resources.limits.memory | quote }}
{{- if .Values.config.runner.extraEnvSecret }}
- name: RUNNER_EXTRA_ENV_SECRET
  value: {{ .Values.config.runner.extraEnvSecret | quote }}
{{- end }}
{{- end }}

{{- define "research.apiSecretEnv" -}}
- name: SERVICE_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "research.secretName" . }}
      key: service-token
- name: SIGNING_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "research.secretName" . }}
      key: signing-secret
- name: OPENWEBUI_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "research.secretName" . }}
      key: openwebui-api-key
{{- end }}

{{- define "research.artifactSecretEnv" -}}
- name: S3_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ include "research.secretName" . }}
      key: s3-access-key-id
      optional: true
- name: S3_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "research.secretName" . }}
      key: s3-secret-access-key
      optional: true
{{- end }}
