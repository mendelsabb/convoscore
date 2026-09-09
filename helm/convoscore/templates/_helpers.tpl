{{/* Chart name, overridable. */}}
{{- define "convoscore.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully qualified release name. */}}
{{- define "convoscore.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "convoscore.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "convoscore.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: convoscore
{{- end -}}

{{/* Selector labels for one component. Usage: (dict "ctx" . "component" "api") */}}
{{- define "convoscore.selectorLabels" -}}
app.kubernetes.io/name: {{ include "convoscore.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "convoscore.postgresHost" -}}
{{- printf "%s-postgres" (include "convoscore.fullname" .) -}}
{{- end -}}

{{/*
Environment shared by every process. Secrets arrive by reference only: the values are never
rendered into the manifest, so `helm template` output contains no credentials.
*/}}
{{- define "convoscore.env" -}}
- name: ENVIRONMENT
  value: {{ .Values.config.environment | quote }}
- name: LOG_LEVEL
  value: {{ .Values.config.logLevel | quote }}
- name: LOG_JSON
  value: {{ .Values.config.logJson | quote }}
- name: DEMO_MODE
  value: {{ .Values.config.demoMode | quote }}
- name: MAX_ATTEMPTS
  value: {{ .Values.config.maxAttempts | quote }}
- name: VISIBILITY_TIMEOUT_SECONDS
  value: {{ .Values.config.visibilityTimeoutSeconds | quote }}
- name: WORKER_WAIT_TIME_SECONDS
  value: {{ .Values.config.workerWaitTimeSeconds | quote }}
- name: RETRY_BASE_BACKOFF_SECONDS
  value: {{ .Values.config.retryBaseBackoffSeconds | quote }}
- name: RETRY_MAX_BACKOFF_SECONDS
  value: {{ .Values.config.retryMaxBackoffSeconds | quote }}
- name: INGEST_POLL_INTERVAL_SECONDS
  value: {{ .Values.config.ingestPollIntervalSeconds | quote }}
- name: AWS_ENDPOINT_URL
  value: {{ .Values.aws.endpointUrl | quote }}
- name: AWS_REGION
  value: {{ .Values.aws.region | quote }}
- name: AWS_ACCESS_KEY_ID
  value: {{ .Values.aws.accessKeyId | quote }}
- name: AWS_SECRET_ACCESS_KEY
  value: {{ .Values.aws.secretAccessKey | quote }}
- name: SQS_QUEUE_NAME
  value: {{ .Values.aws.queueName | quote }}
- name: S3_BUCKET
  value: {{ .Values.aws.bucket | quote }}
- name: S3_PREFIX
  value: {{ .Values.aws.prefix | quote }}
- name: LLM_PROVIDER
  value: {{ .Values.llm.provider | quote }}
- name: OPENAI_MODEL
  value: {{ .Values.llm.model | quote }}
- name: OPENAI_TIMEOUT_SECONDS
  value: {{ .Values.llm.timeoutSeconds | quote }}
{{- if kindIs "invalid" .Values.llm.temperature }}
{{- else }}
- name: OPENAI_TEMPERATURE
  value: {{ .Values.llm.temperature | quote }}
{{- end }}
- name: POSTGRES_HOST
  value: {{ include "convoscore.postgresHost" . | quote }}
- name: POSTGRES_PORT
  value: "5432"
- name: POSTGRES_DB
  value: {{ .Values.postgres.database | quote }}
- name: POSTGRES_USER
  value: {{ .Values.postgres.username | quote }}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.postgres.existingSecret }}
      key: {{ .Values.postgres.passwordSecretKey }}
{{- if eq .Values.llm.provider "openai" }}
- name: OPENAI_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.llm.existingSecret }}
      key: {{ .Values.llm.apiKeySecretKey }}
{{- end }}
{{- end -}}

{{/*
Pod hardening applied to every application container: no root, no privilege escalation, no
writable filesystem, no capabilities. A writable /tmp is mounted where a process needs one.
*/}}
{{- define "convoscore.securityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
runAsUser: 10001
capabilities:
  drop:
    - ALL
{{- end -}}
