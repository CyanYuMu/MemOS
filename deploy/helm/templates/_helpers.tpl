{{- define "memos.name" -}}
memos
{{- end }}

{{- define "memos.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "memos.labels" -}}
app.kubernetes.io/name: {{ include "memos.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion }}
{{- end }}

{{/*
Deployment 的 spec.selector 在 apps/v1 下不可变。绝不要把 appVersion 放进来：
改一次 Chart.yaml 的 appVersion，helm upgrade 就会以 "field is immutable" 失败，
只能删掉 Deployment 重建。selector 只用 name + instance，version 留在 metadata/pod labels。
*/}}
{{- define "memos.selectorLabels" -}}
app.kubernetes.io/name: {{ include "memos.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "memos.neo4j.fullname" -}}
{{- printf "%s-neo4j" (include "memos.fullname" .) }}
{{- end }}

{{- define "memos.qdrant.fullname" -}}
{{- printf "%s-qdrant" (include "memos.fullname" .) }}
{{- end }}

{{- define "memos.redis.fullname" -}}
{{- printf "%s-redis" (include "memos.fullname" .) }}
{{- end }}

{{- define "memos.memos.fullname" -}}
{{- printf "%s-api" (include "memos.fullname" .) }}
{{- end }}

{{- define "memos.secretName" -}}
{{- if .Values.memos.secret.name }}
{{- .Values.memos.secret.name }}
{{- else }}
{{- printf "%s-secret" (include "memos.memos.fullname" .) }}
{{- end }}
{{- end }}

{{- define "memos.pvcName" -}}
{{- if .Values.memos.persistence.existingClaim }}
{{- .Values.memos.persistence.existingClaim }}
{{- else }}
{{- printf "%s-data" (include "memos.memos.fullname" .) }}
{{- end }}
{{- end }}

{{/*
Environment rendered into the ConfigMap. In-cluster dependencies override the
matching keys so the same values file works with or without the sub-charts.
Empty values are dropped rather than rendered as "": an empty NEO4J_URI would
otherwise override the application's own default with nothing.
*/}}
{{- define "memos.env" -}}
{{- $env := deepCopy (default dict .Values.memos.env) -}}
{{- if .Values.neo4j.enabled -}}
{{- $_ := set $env "NEO4J_URI" (printf "bolt://%s:%v" (include "memos.neo4j.fullname" .) .Values.neo4j.service.boltPort) -}}
{{- if .Values.neo4j.auth.enabled -}}
{{- $_ := set $env "NEO4J_USER" .Values.neo4j.auth.user -}}
{{- end -}}
{{- end -}}
{{- if .Values.qdrant.enabled -}}
{{- $_ := set $env "QDRANT_HOST" (include "memos.qdrant.fullname" .) -}}
{{- $_ := set $env "QDRANT_PORT" (toString .Values.qdrant.service.httpPort) -}}
{{- end -}}
{{- if .Values.redis.enabled -}}
{{- $_ := set $env "MEMSCHEDULER_REDIS_HOST" (include "memos.redis.fullname" .) -}}
{{- $_ := set $env "MEMSCHEDULER_REDIS_PORT" (toString .Values.redis.service.port) -}}
{{- end -}}
{{- $out := dict -}}
{{- range $k, $v := $env -}}
{{- $s := (default "" $v) | toString -}}
{{- if $s -}}
{{- $_ := set $out $k $s -}}
{{- end -}}
{{- end -}}
{{- toYaml $out -}}
{{- end }}
