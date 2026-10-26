"""Generates Grafana dashboards to monitor the app, containers, and Azure resources.
Keeps the metrics and dashboard layouts consistent across environments.
"""

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prometheus"}
# Only processes that pushed recently (a killed process leaves its last values in the Pushgateway)
FRESH = '(time() - push_time_seconds{job="meteo-backend"}) < 60'
API = 'route=~"/api/.*"'

panels = []
_next_id = [1]


def _id():
    _next_id[0] += 1
    return _next_id[0]


def row(title, y):
    panels.append({"type": "row", "title": title, "id": _id(), "collapsed": False,
                   "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []})


def target(expr, legend="", ref="A"):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref, "range": True}


def stat(title, expr, x, y, unit="short", w=6, decimals=None, thresholds=None, description=""):
    steps = thresholds or [{"color": "green", "value": None}]
    defaults = {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}, "color": {"mode": "thresholds"}}
    if decimals is not None:
        defaults["decimals"] = decimals
    panels.append({
        "type": "stat", "title": title, "id": _id(), "datasource": DS, "description": description,
        "gridPos": {"x": x, "y": y, "w": w, "h": 4},
        "targets": [target(expr)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "area", "textMode": "value", "justifyMode": "center"},
    })


def series(title, targets, x, y, unit="short", w=12, h=8, stack=False, description="", max_=None):
    custom = {"lineWidth": 2, "fillOpacity": 18 if stack else 8, "showPoints": "never",
              "stacking": {"mode": "normal" if stack else "none", "group": "A"}}
    defaults = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"}}
    if max_ is not None:
        defaults["max"] = max_
    panels.append({
        "type": "timeseries", "title": title, "id": _id(), "datasource": DS, "description": description,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(e, legend, chr(65 + i)) for i, (e, legend) in enumerate(targets)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
    })


# --- Overview ----------------------------------------------------------------------------------
stat("Requêtes / s (API)", f"sum(rate(meteo_http_requests_total{{{API}}}[1m])) or vector(0)", 0, 0, "reqps", decimals=1,
     description="Requêtes /api/* traitées par seconde, tous réplicas confondus")
stat("Erreurs 5xx", f'100 * (sum(rate(meteo_http_requests_total{{{API},status=~"5.."}}[5m])) or vector(0))'
     f" / clamp_min(sum(rate(meteo_http_requests_total{{{API}}}[5m])), 1e-9)", 6, 0, "percent", decimals=2,
     thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.5}, {"color": "red", "value": 1}],
     description="Part des réponses 5xx sur 5 minutes. Objectif : moins de 1 %")
stat("Latence p95 (API)", f"histogram_quantile(0.95, sum by (le) (rate(meteo_http_request_duration_seconds_bucket{{{API}}}[5m])))",
     12, 0, "s", decimals=3,
     thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.5}, {"color": "red", "value": 1.5}],
     description="95 % des requêtes /api/* sont traitées en moins de cette durée (côté backend)")
stat("Réplicas backend", f"count(count by (replica) ({FRESH})) or vector(0)", 18, 0, "none",
     thresholds=[{"color": "blue", "value": None}, {"color": "orange", "value": 8}],
     description="Réplicas qui ont poussé leurs métriques il y a moins de 60 s (plafond : 8)")

# --- Traffic and latency -----------------------------------------------------------------------
row("Trafic et latence", 4)
series("Requêtes / s par route", [(f"sum by (route) (rate(meteo_http_requests_total{{{API}}}[1m]))", "{{route}}")],
       0, 5, "reqps")
series("Latence par route", [
    (f"histogram_quantile(0.95, sum by (le, route) (rate(meteo_http_request_duration_seconds_bucket{{{API}}}[5m])))", "p95 {{route}}"),
    (f"histogram_quantile(0.99, sum by (le) (rate(meteo_http_request_duration_seconds_bucket{{{API}}}[5m])))", "p99 toutes routes"),
], 12, 5, "s")
series("Réponses par code HTTP", [(f"sum by (status) (rate(meteo_http_requests_total{{{API}}}[1m]))", "{{status}}")],
       0, 13, "reqps", stack=True)
series("Réplicas et processus du backend", [
    (f"count(count by (replica) ({FRESH}))", "réplicas"),
    (f"count({FRESH})", "processus gunicorn"),
], 12, 13, "none", description="Chaque réplica fait tourner 2 processus gunicorn, chacun pousse ses métriques")

# --- Open-Meteo and resilience -----------------------------------------------------------------
row("Dépendance Open-Meteo et résilience", 21)
series("Appels à Open-Meteo par résultat", [("sum by (api, outcome) (rate(meteo_dependency_calls_total[1m]))", "{{api}} : {{outcome}}")],
       0, 22, "reqps", description="circuit_open = appel refusé sans essayer (disjoncteur ouvert)")
series("Latence d'Open-Meteo (p95)", [("histogram_quantile(0.95, sum by (le, api) (rate(meteo_dependency_duration_seconds_bucket[5m])))", "{{api}}")],
       12, 22, "s")
series("Disjoncteurs ouverts", [(f'max by (api) (meteo_circuit_open and on (job, replica, worker) ({FRESH}))', "{{api}}")],
       0, 30, "none", w=8, max_=1, description="1 = au moins un processus refuse les appels à cette API")
series("Origine de la météo servie", [("sum by (source) (rate(meteo_weather_source_total[5m]))", "{{source}}")],
       8, 30, "reqps", w=8, stack=True,
       description="cache : sans appel externe ; live : rafraîchie ; refreshing / stale : dernière valeur connue")
series("Suggestions par niveau de cache", [("sum by (level) (rate(meteo_suggestions_total[5m]))", "{{level}}")],
       16, 30, "reqps", w=8, stack=True,
       description="memory : processus ; shared : base (partagé entre réplicas) ; api : appel à Open-Meteo")

# --- Database and resources --------------------------------------------------------------------
row("Base de données et ressources", 38)
series("Pool de connexions PostgreSQL (tous processus)", [
    (f'sum by (state) (meteo_db_pool and on (job, replica, worker) ({FRESH}))', "{{state}}")],
    0, 39, "none", w=8, description="waiting > 0 : des requêtes attendent une connexion, la base devient le goulot")
series("CPU par réplica", [(f'sum by (replica) (rate(process_cpu_seconds_total{{job="meteo-backend"}}[1m]))', "{{replica}}")],
       8, 39, "percentunit", w=8)
series("Mémoire par réplica", [(f'sum by (replica) (process_resident_memory_bytes and on (job, replica, worker) ({FRESH}))', "{{replica}}")],
       16, 39, "bytes", w=8)

# --- Load test (k6) -------------------------------------------------------------------------------
row("Test de charge (k6, envoyé par remote write)", 47)
series("Utilisateurs virtuels", [("sum(k6_vus)", "VUs")], 0, 48, "none", w=8)
series("Requêtes envoyées par k6 / s", [("sum(rate(k6_http_reqs_total[1m]))", "requêtes / s")], 8, 48, "reqps", w=8)
series("Latence vue par k6 (p95, p99)", [
    ("max(k6_http_req_duration_p95)", "p95"),
    ("max(k6_http_req_duration_p99)", "p99"),
], 16, 48, "s", w=8, description="Temps de réponse mesuré côté client (réseau compris)")

def save(name, uid, title, tags, panel_list, links=None):
    dashboard = {
        "uid": uid,
        "title": title,
        "tags": tags,
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": False,
        "refresh": "10s",
        "time": {"from": "now-30m", "to": "now"},
        "panels": panel_list,
        "templating": {"list": []},
        "annotations": {"list": []},
        "links": links or [],
    }
    out = Path(__file__).parent / "dashboards" / name
    out.write_text(json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{out.name}: {len(panel_list)} panels")


LINKS = [{"type": "dashboards", "tags": ["meteo"], "asDropdown": False, "title": "Météo des villes", "includeVars": False,
          "keepTime": True}]

save("meteo.json", "meteo-sre", "Météo des villes — SRE", ["meteo", "sre"], panels, LINKS)

# --- Local containers: cAdvisor -------------------------------------------------------------------
panels = []
SVC = "container_label_com_docker_compose_service"
SEL = f'{SVC}=~"frontend|backend|db"'
series("CPU par conteneur", [(f"sum by ({SVC}) (rate(container_cpu_usage_seconds_total{{{SEL}}}[1m]))", f"{{{{{SVC}}}}}")],
       0, 0, "percentunit", description="Part d'un cœur utilisée par chaque service (cAdvisor)")
series("Mémoire par conteneur (working set)", [(f"sum by ({SVC}) (container_memory_working_set_bytes{{{SEL}}})", f"{{{{{SVC}}}}}")],
       12, 0, "bytes", description="Mémoire réellement utilisée, celle que surveille le noyau avant un OOM kill")
series("Réseau reçu par conteneur", [(f"sum by ({SVC}) (rate(container_network_receive_bytes_total{{{SEL}}}[1m]))", f"{{{{{SVC}}}}}")],
       0, 8, "Bps")
series("Réseau envoyé par conteneur", [(f"sum by ({SVC}) (rate(container_network_transmit_bytes_total{{{SEL}}}[1m]))", f"{{{{{SVC}}}}}")],
       12, 8, "Bps")
save("conteneurs-local.json", "meteo-conteneurs-local", "Conteneurs — local (cAdvisor)", ["meteo", "conteneurs"],
     panels, LINKS)

# --- Containers on Azure: Azure Monitor -----------------------------------------------------------
# No host access on Container Apps, so no cAdvisor: the platform provides per-app metrics, read by
# Grafana's managed identity. __AZURE_SUBSCRIPTION_ID__ is replaced when the image is built.
AZ = {"type": "grafana-azure-monitor-datasource", "uid": "azure-monitor"}
SUB = "__AZURE_SUBSCRIPTION_ID__"
NS = "microsoft.app/containerapps"


def az_target(metric, aggregation, app, ref):
    # One query per app: a multi-resource query uses the subscription-level API, which our
    # resource-group-scoped Monitoring Reader role cannot call
    return {
        "refId": ref, "datasource": AZ, "queryType": "Azure Monitor", "subscription": SUB,
        "azureMonitor": {
            "resources": [{"subscription": SUB, "resourceGroup": "rg-sre-meteo", "metricNamespace": NS,
                           "resourceName": app, "region": "francecentral"}],
            "metricNamespace": NS, "metricName": metric, "aggregation": aggregation, "timeGrain": "auto",
            "region": "francecentral", "dimensionFilters": [], "alias": "{{ resourcename }}",
        },
    }


def az_series(title, metric, aggregation, x, y, unit, description="", w=12):
    panels.append({
        "type": "timeseries", "title": title, "id": _id(), "datasource": AZ, "description": description,
        "gridPos": {"x": x, "y": y, "w": w, "h": 8},
        "targets": [az_target(metric, aggregation, app, ref) for ref, app in (("A", "meteo-frontend"), ("B", "meteo-backend"))],
        "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": "palette-classic"},
                                     "custom": {"lineWidth": 2, "fillOpacity": 8, "showPoints": "never"}},
                        "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
    })


panels = []
az_series("Réplicas (frontend, backend)", "Replicas", "Maximum", 0, 0, "none",
          "Nombre de réplicas en cours d'exécution : l'autoscaling vu par la plateforme")
az_series("Redémarrages de conteneurs", "RestartCount", "Maximum", 12, 0, "none",
          "Un redémarrage = plantage ou sonde en échec")
az_series("CPU (nanocœurs, moyenne par réplica)", "UsageNanoCores", "Average", 0, 8, "short",
          "1 000 000 000 nanocœurs = 1 vCPU. Le backend a 0,5 vCPU par réplica, le frontend 0,25")
az_series("Mémoire (working set, moyenne par réplica)", "WorkingSetBytes", "Average", 12, 8, "bytes",
          "Limites : 1 Go par réplica backend, 0,5 Go par réplica frontend")
az_series("Réseau reçu", "RxBytes", "Total", 0, 16, "bytes")
az_series("Réseau envoyé", "TxBytes", "Total", 12, 16, "bytes")
save("conteneurs-azure.json", "meteo-conteneurs-azure", "Conteneurs — Azure (Azure Monitor)", ["meteo", "conteneurs"],
     panels, LINKS)
