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

{{- define "research.apiServiceAccount" -}}
{{- default (printf "%s-api" (include "research.fullname" .)) .Values.api.serviceAccount.name }}
{{- end }}

{{- define "research.researchJobServiceAccount" -}}
{{- default (printf "%s-research-job" (include "research.fullname" .)) .Values.researchJob.serviceAccount.name }}
{{- end }}

{{- define "research.searxngFullname" -}}
{{- printf "%s-searxng" (include "research.fullname" . | trunc 55 | trimSuffix "-") -}}
{{- end }}
