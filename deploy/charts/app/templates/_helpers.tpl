{{- define "app.name" -}}{{ .Release.Name }}{{- end -}}
{{- define "app.labels" -}}
app.kubernetes.io/name: {{ include "app.name" . }}
app.kubernetes.io/part-of: shop
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
{{- end -}}
{{- define "app.selector" -}}
app.kubernetes.io/name: {{ include "app.name" . }}
{{- end -}}
