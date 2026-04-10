{{/*
Expand the name of the chart.
*/}}
{{- define "worker.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "worker.fullname" -}}
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
Construct a full image reference from registry + image + tag.
Usage: {{ include "worker.image" (dict "registry" $registry "image" .Values.image "tag" .Values.tag) }}
*/}}
{{- define "worker.image" -}}
{{- if .registry -}}
{{- printf "%s/%s:%s" .registry .image .tag -}}
{{- else -}}
{{- printf "%s:%s" .image .tag -}}
{{- end -}}
{{- end }}

{{/*
Common labels applied to every resource.
*/}}
{{- define "worker.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels for a given component.
Usage: {{ include "worker.selectorLabels" (dict "Release" .Release "component" "worker") }}
*/}}
{{- define "worker.selectorLabels" -}}
app.kubernetes.io/name: {{ .component }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
