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
