{{- /* 通用标签 */ -}}
{{- define "pdm.labels" -}}
app.kubernetes.io/name: diting-pdm
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
pdm.mode: {{ .Values.mode }}
{{- end -}}

{{- define "pdm.env" -}}
- name: PDM_ENV
  value: {{ if eq .Values.mode "edge" }}edge{{ else }}prod{{ end }}
- name: PDM_DB_BACKEND
  value: {{ .Values.database.backend | quote }}
{{- if eq .Values.database.backend "sqlite" }}
- name: PDM_SQLITE_PATH
  value: {{ .Values.database.sqlitePath | quote }}
{{- else }}
- name: PDM_TIDB_HOST
  value: {{ .Values.database.tidb.host | quote }}
- name: PDM_TIDB_PORT
  value: {{ .Values.database.tidb.port | quote }}
- name: PDM_TIDB_USER
  value: {{ .Values.database.tidb.user | quote }}
- name: PDM_TIDB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.tidb.secretName }}
      key: {{ .Values.database.tidb.secretKey }}
      optional: true
{{- end }}
- name: PDM_AUTH_REQUIRED
  value: {{ .Values.auth.required | quote }}
- name: PDM_DASHBOARD_USER
  valueFrom:
    secretKeyRef:
      name: {{ if eq .Values.mode "edge" }}{{ .Values.auth.edgeSecretName }}{{ else }}{{ .Values.auth.secretName }}{{ end }}
      key: username
      optional: true
- name: PDM_DASHBOARD_PASS
  valueFrom:
    secretKeyRef:
      name: {{ if eq .Values.mode "edge" }}{{ .Values.auth.edgeSecretName }}{{ else }}{{ .Values.auth.secretName }}{{ end }}
      key: password
      optional: true
{{- if .Values.observability.otlpEndpoint }}
- name: PDM_OTLP_ENDPOINT
  value: {{ .Values.observability.otlpEndpoint | quote }}
{{- end }}
{{- end -}}
