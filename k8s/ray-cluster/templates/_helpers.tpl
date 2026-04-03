{{/*
Full image reference: <region>-docker.pkg.dev/<project>/<repo>/ray-pipeline:<tag>
*/}}
{{- define "ray-cluster.image" -}}
{{- printf "%s-docker.pkg.dev/%s/%s/ray-pipeline:%s" .Values.global.garRegion .Values.global.projectId .Values.global.garRepo .Values.image.tag -}}
{{- end }}

{{/*
GPU image reference for train workers (includes CUDA + sentence-transformers).
*/}}
{{- define "ray-cluster.imageGPU" -}}
{{- printf "%s-docker.pkg.dev/%s/%s/ray-pipeline:%s" .Values.global.garRegion .Values.global.projectId .Values.global.garRepo .Values.image.gpuTag -}}
{{- end }}

{{/*
Nessie warehouse name passed to the Iceberg REST catalog client.
The GCS location for this name is configured server-side in Nessie via
NESSIE_CATALOG_WAREHOUSES__WAREHOUSE__LOCATION in nessie-values.yaml.
*/}}
{{- define "ray-cluster.icebergWarehouse" -}}
warehouse
{{- end }}
