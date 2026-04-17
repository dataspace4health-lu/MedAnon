{{/*
Expand the name of the chart.
*/}}
{{- define "gpas.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "gpas.fullname" -}}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Fully qualified name for the PostgreSQL database service.
Used as the DB hostname for TTP_DB_HOST, TTP_GRAS_DB_HOST, and MOS_WAIT_FOR_PORTS.
*/}}
{{- define "gpas.dbHost" -}}
{{- printf "%s-db" (include "gpas.fullname" .) -}}
{{- end }}

{{/*
Construct a full image reference from registry + image + tag.
Usage: {{ include "gpas.image" (dict "registry" $registry "image" .Values.image "tag" .Values.tag) }}
*/}}
{{- define "gpas.image" -}}
{{- if .registry -}}
{{- printf "%s/%s:%s" .registry .image .tag -}}
{{- else -}}
{{- printf "%s:%s" .image .tag -}}
{{- end -}}
{{- end }}

{{/*
Common labels applied to every resource.
*/}}
{{- define "gpas.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels for a given component.
Usage: {{ include "gpas.selectorLabels" (dict "Release" .Release "component" "gpas") }}
*/}}
{{- define "gpas.selectorLabels" -}}
app.kubernetes.io/name: {{ .component }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
