# Divine Osprey Integration — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deploy Osprey as Divine's behavioral rules engine with ClickHouse replacing Druid, Kafka for event ingestion, and SML rules for Nostr event moderation.

**Architecture:** Fork of roostorg/osprey at github.com/divinevideo/osprey. ClickHouse replaces Druid for query/storage. Kafka (Strimzi) on GKE for event input. Osprey evaluates SML rules against Nostr events and outputs verdicts via effect sinks to relay-manager NIP-86, Zendesk, and push notifications. All infra managed via divine-iac-coreconfig with ArgoCD + Kustomize overlays.

**Tech Stack:** Python (Osprey workers, UDFs, SML rules), Rust (Osprey coordinator), ClickHouse, Kafka (Strimzi), Postgres (CloudNativePG), GKE, ArgoCD, Kustomize

**Repo:** `github.com/divinevideo/osprey` — branch `divine/clickhouse-adapter` (ClickHouse adapters already pushed)

**Already Done:**
- ✅ Fork created at divinevideo/osprey
- ✅ `ClickHouseOutputSink` — batch inserts to CH
- ✅ `ast_clickhouse_translator.py` — AST → SQL WHERE
- ✅ `clickhouse.py` — query backend (timeseries, topN, scan, groupBy)
- ✅ CH schema with MergeTree + materialized views
- ✅ Sink registration with CH config
- ✅ `clickhouse-connect` dependency

---

## Task 1: Wire ClickHouse into UI API Views (replace Druid imports)

**Context:** The Osprey UI API views currently import from `druid.py`. We need to swap those imports to use `clickhouse.py` instead, and replace the `DRUID` singleton with a `CLICKHOUSE` singleton.

**Files:**
- Create: `osprey_worker/src/osprey/worker/ui_api/osprey/lib/clickhouse_client_holder.py`
- Modify: `osprey_worker/src/osprey/worker/ui_api/osprey/singletons.py`
- Modify: `osprey_worker/src/osprey/worker/ui_api/osprey/views/events.py`
- Modify: `osprey_worker/src/osprey/worker/ui_api/osprey/views/queries.py`
- Modify: `osprey_worker/src/osprey/worker/ui_api/osprey/validators/events.py`
- Modify: `osprey_worker/src/osprey/worker/ui_api/osprey/validators/entities.py`
- Modify: `osprey_worker/src/osprey/worker/ui_api/osprey/lib/abilities.py`
- Modify: `osprey_worker/src/osprey/worker/ui_api/osprey/cli.py`

**Step 1:** Create `clickhouse_client_holder.py` — singleton that initializes clickhouse-connect client from config:

```python
import clickhouse_connect
from osprey.worker.lib.singletons import CONFIG

class ClickHouseClientHolder:
    def __init__(self):
        config = CONFIG.instance()
        self._client = clickhouse_connect.get_client(
            host=config.expect_str('CLICKHOUSE_HOST'),
            port=config.get_int('CLICKHOUSE_PORT', 8123),
            username=config.get_str('CLICKHOUSE_USER', 'default'),
            password=config.get_str('CLICKHOUSE_PASSWORD', ''),
        )
        self._database = config.get_str('CLICKHOUSE_DATABASE', 'osprey')
        self._table = config.get_str('CLICKHOUSE_TABLE', 'osprey_events')

    @property
    def client(self):
        return self._client

    @property
    def database(self):
        return self._database

    @property
    def table(self):
        return self._table
```

**Step 2:** Update `singletons.py` — add CLICKHOUSE singleton alongside DRUID (keep DRUID for backward compat):

```python
from .lib.clickhouse_client_holder import ClickHouseClientHolder
CLICKHOUSE: Singleton[ClickHouseClientHolder] = Singleton(ClickHouseClientHolder)
```

**Step 3:** Update `views/events.py` — replace Druid query imports with ClickHouse equivalents. The query classes have the same interface (execute() returns same response types), so the view logic stays identical. Key change: query classes need the ClickHouse backend passed in.

**Step 4:** Update `views/queries.py` — replace `parse_query_filter` import to use ClickHouse translator.

**Step 5:** Update `validators/events.py` and `validators/entities.py` — change base classes from Druid to ClickHouse query types.

**Step 6:** Update `lib/abilities.py` — change `BaseDruidQuery` import to `BaseClickHouseQuery`.

**Step 7:** Update `cli.py` — replace DRUID_URL config with CLICKHOUSE_HOST config.

**Step 8:** Commit:
```bash
git add -A
git commit -m "feat: wire ClickHouse backend into UI API views, replacing Druid"
```

---

## Task 2: Divine Plugin Package — UDFs and Effect Sinks

**Context:** Divine needs custom UDFs for Nostr-specific operations and effect sinks that call relay-manager's NIP-86 API.

**Files:**
- Create: `divine/plugins/src/register_plugins.py`
- Create: `divine/plugins/src/udfs/__init__.py`
- Create: `divine/plugins/src/udfs/ban_nostr_event.py` — effect UDF: emit NIP-86 banevent RPC
- Create: `divine/plugins/src/udfs/nostr_account_age.py` — UDF: calculate account age from kind 0 created_at
- Create: `divine/plugins/src/udfs/check_moderation_result.py` — UDF: query moderation-service KV for existing scan results
- Create: `divine/plugins/src/services/__init__.py`
- Create: `divine/plugins/src/services/relay_manager_sink.py` — output sink calling relay-manager NIP-86
- Create: `divine/plugins/src/services/zendesk_sink.py` — output sink creating/updating Zendesk tickets

**Step 1:** Create `ban_nostr_event.py` — Effect UDF that emits a `BanEventEffect` consumed by the relay-manager output sink:

```python
class BanEventEffect(EffectBase):
    event_id: str
    pubkey: str
    reason: str

class BanNostrEventArguments(ArgumentsBase):
    event_id: str
    pubkey: str
    reason: str

class BanNostrEvent(UDFBase[BanNostrEventArguments, BanEventEffect]):
    category = UdfCategories.ENGINE
    def execute(self, ctx, args):
        return BanEventEffect(event_id=args.event_id, pubkey=args.pubkey, reason=args.reason)
```

**Step 2:** Create `relay_manager_sink.py` — output sink that consumes `BanEventEffect` and calls relay-manager NIP-86:

```python
class RelayManagerOutputSink(BaseOutputSink):
    def push(self, result):
        for effect in result.effects.get(BanEventEffect, []):
            # POST to relay-manager NIP-86 banevent RPC
            requests.post(self._relay_manager_url, json={
                "method": "banevent",
                "params": {"event_id": effect.event_id, "reason": effect.reason}
            })
```

**Step 3:** Create `register_plugins.py` — register all Divine UDFs and sinks:

```python
@hookimpl_osprey
def register_udfs():
    return [BanNostrEvent, NostrAccountAge, CheckModerationResult]

@hookimpl_osprey
def register_output_sinks(config):
    return [RelayManagerOutputSink(config), ZendeskOutputSink(config)]
```

**Step 4:** Commit:
```bash
git commit -m "feat: add Divine plugin package with Nostr UDFs and relay-manager sink"
```

---

## Task 3: Nostr Event SML Models

**Context:** SML models define how Osprey extracts features from incoming Nostr event JSON. We need models for the core Nostr event types that Divine processes.

**Files:**
- Create: `divine/rules/models/base.sml` — common Nostr event fields
- Create: `divine/rules/models/nostr/kind0_metadata.sml` — profile metadata
- Create: `divine/rules/models/nostr/kind1_note.sml` — text notes
- Create: `divine/rules/models/nostr/kind1984_report.sml` — NIP-56 reports
- Create: `divine/rules/models/nostr/video_event.sml` — NIP-71 video events
- Create: `divine/rules/main.sml` — entry point

**Step 1:** Create `base.sml` — Nostr events always have id, pubkey, kind, created_at, content, tags:

```python
EventId: Entity[str] = EntityJson(type='EventId', path='$.id')
Pubkey: Entity[str] = EntityJson(type='Pubkey', path='$.pubkey')
Kind: int = JsonData(path='$.kind')
CreatedAt: int = JsonData(path='$.created_at')
Content: str = JsonData(path='$.content', required=False)
Tags: List[str] = JsonData(path='$.tags', required=False)
ActionName = GetActionName()
```

**Step 2:** Create `kind1_note.sml` — text note features:

```python
Import(rules=['models/base.sml'])
NoteText: str = JsonData(path='$.content')
# Extract p-tags (mentioned pubkeys)
MentionedPubkeys: List[str] = JsonData(path='$.tags[?(@[0]=="p")][1]', required=False)
```

**Step 3:** Create `kind1984_report.sml` — report event features:

```python
Import(rules=['models/base.sml'])
ReportedEventId: str = JsonData(path='$.tags[?(@[0]=="e")][1]', required=False)
ReportedPubkey: str = JsonData(path='$.tags[?(@[0]=="p")][1]', required=False)
ReportReason: str = JsonData(path='$.tags[?(@[0]=="report")][1]', required=False)
```

**Step 4:** Create `video_event.sml` — video (kind 34235/34236) features:

```python
Import(rules=['models/base.sml'])
VideoUrl: str = JsonData(path='$.tags[?(@[0]=="url")][1]', required=False)
VideoHash: str = JsonData(path='$.tags[?(@[0]=="x")][1]', required=False)
VideoTitle: str = JsonData(path='$.tags[?(@[0]=="title")][1]', required=False)
```

**Step 5:** Create `main.sml`:

```python
Import(rules=['models/base.sml'])
Require(rule='rules/behavioral/index.sml')
Require(rule='rules/content/index.sml', require_if=Kind in [34235, 34236])
Require(rule='rules/reports/index.sml', require_if=Kind == 1984)
```

**Step 6:** Commit:
```bash
git commit -m "feat: add Nostr event SML models for Divine"
```

---

## Task 4: First Behavioral SML Rules

**Context:** Write the initial behavioral detection rules that address Divine's PRD gaps: spam, graduated enforcement, trusted reporter auto-hide, and invite abuse.

**Files:**
- Create: `divine/rules/rules/behavioral/index.sml`
- Create: `divine/rules/rules/behavioral/new_account_spam.sml`
- Create: `divine/rules/rules/behavioral/repeat_offender.sml`
- Create: `divine/rules/rules/behavioral/trusted_reporter.sml`
- Create: `divine/rules/rules/reports/index.sml`
- Create: `divine/rules/rules/reports/auto_hide.sml`
- Create: `divine/rules/config/labels.yaml` — label definitions

**Step 1:** Create `new_account_spam.sml`:

```python
Import(rules=['models/base.sml', 'models/nostr/kind1_note.sml'])

NewAccountRapidPost = Rule(
    when_all=[
        Kind == 1,
        NostrAccountAge(pubkey=Pubkey) < TimeDelta(hours=1),
        not HasLabel(entity=Pubkey, label='verified'),
    ],
    description=f"New account {Pubkey} posting within first hour",
)

WhenRules(
    rules_any=[NewAccountRapidPost],
    then=[
        DeclareVerdict(verdict='flag_for_review'),
        LabelAdd(entity=Pubkey, label='new_account_activity', expires_after=TimeDelta(days=7)),
    ],
)
```

**Step 2:** Create `repeat_offender.sml` — graduated enforcement:

```python
Import(rules=['models/base.sml'])

PreviouslyWarned = Rule(
    when_all=[HasLabel(entity=Pubkey, label='warned')],
    description=f"User {Pubkey} was previously warned",
)

PreviouslySuspended = Rule(
    when_all=[HasLabel(entity=Pubkey, label='suspended')],
    description=f"User {Pubkey} was previously suspended",
)

# Escalation: warned → suspend
WhenRules(
    rules_any=[PreviouslyWarned],
    then=[
        LabelAdd(entity=Pubkey, label='suspended', expires_after=TimeDelta(days=30)),
        LabelRemove(entity=Pubkey, label='warned'),
        DeclareVerdict(verdict='suspend'),
    ],
)

# Escalation: suspended → ban
WhenRules(
    rules_any=[PreviouslySuspended],
    then=[
        LabelAdd(entity=Pubkey, label='banned'),
        BanNostrEvent(event_id=EventId, pubkey=Pubkey, reason='Repeat offender — escalated to ban'),
    ],
)
```

**Step 3:** Create `trusted_reporter.sml` — auto-hide on report from trusted client:

```python
Import(rules=['models/base.sml', 'models/nostr/kind1984_report.sml'])

TrustedReporterCSAM = Rule(
    when_all=[
        Kind == 1984,
        HasLabel(entity=Pubkey, label='trusted_reporter'),
        ReportReason == 'csam',
    ],
    description=f"CSAM report from trusted reporter {Pubkey}",
)

WhenRules(
    rules_any=[TrustedReporterCSAM],
    then=[
        BanNostrEvent(event_id=ReportedEventId, pubkey=ReportedPubkey, reason='CSAM report from trusted reporter'),
        DeclareVerdict(verdict='auto_hide'),
    ],
)
```

**Step 4:** Create label definitions in `config/labels.yaml`:

```yaml
labels:
  new_account_activity:
    valid_for: [Pubkey]
    connotation: neutral
    description: "New account with recent activity"
  warned:
    valid_for: [Pubkey]
    connotation: negative
    description: "User has been warned"
  suspended:
    valid_for: [Pubkey]
    connotation: negative
    description: "User is suspended"
  banned:
    valid_for: [Pubkey]
    connotation: negative
    description: "User is banned"
  verified:
    valid_for: [Pubkey]
    connotation: positive
    description: "Verified user"
  trusted_reporter:
    valid_for: [Pubkey]
    connotation: positive
    description: "Trusted reporter client"
```

**Step 5:** Wire up index files and commit:
```bash
git commit -m "feat: add initial behavioral SML rules for Divine T&S"
```

---

## Task 5: K8s Manifests for divine-iac-coreconfig — Strimzi Kafka

**Context:** Deploy Kafka on GKE using Strimzi operator, following divine-iac-coreconfig's existing patterns (ArgoCD apps + Kustomize base/overlays for poc/staging/production).

**Repo:** `github.com/divinevideo/divine-iac-coreconfig` (clone separately)

**Files:**
- Create: `k8s/argocd/apps/strimzi-operator.yaml`
- Create: `k8s/argocd/apps/osprey-kafka.yaml`
- Create: `k8s/applications/strimzi/base/kustomization.yaml`
- Create: `k8s/applications/strimzi/base/namespace.yaml`
- Create: `k8s/applications/osprey-kafka/base/kustomization.yaml`
- Create: `k8s/applications/osprey-kafka/base/kafka-cluster.yaml`
- Create: `k8s/applications/osprey-kafka/base/kafka-topics.yaml`
- Create: `k8s/applications/osprey-kafka/overlays/poc/kustomization.yaml`
- Create: `k8s/applications/osprey-kafka/overlays/staging/kustomization.yaml`
- Create: `k8s/applications/osprey-kafka/overlays/production/kustomization.yaml`

**Step 1:** Create Strimzi operator ArgoCD app (Helm-based):

```yaml
# strimzi-operator.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: strimzi-operator
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://strimzi.io/charts/
    chart: strimzi-kafka-operator
    targetRevision: 0.44.0
    helm:
      values: |
        watchNamespaces: ["osprey"]
  destination:
    server: https://kubernetes.default.svc
    namespace: osprey
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

**Step 2:** Create Kafka cluster manifest — small for poc, larger for production:

```yaml
# kafka-cluster.yaml
apiVersion: kafka.strimzi.io/v1beta2
kind: Kafka
metadata:
  name: osprey-kafka
  namespace: osprey
spec:
  kafka:
    replicas: 1  # override in production overlay
    listeners:
      - name: plain
        port: 9092
        type: internal
        tls: false
    storage:
      type: persistent-claim
      size: 10Gi  # override in production
    config:
      offsets.topic.replication.factor: 1
      transaction.state.log.replication.factor: 1
  zookeeper:
    replicas: 1
    storage:
      type: persistent-claim
      size: 5Gi
```

**Step 3:** Create Kafka topics:

```yaml
# kafka-topics.yaml
apiVersion: kafka.strimzi.io/v1beta2
kind: KafkaTopic
metadata:
  name: nostr-events
  namespace: osprey
  labels:
    strimzi.io/cluster: osprey-kafka
spec:
  partitions: 3
  replicas: 1
  config:
    retention.ms: 604800000  # 7 days
---
apiVersion: kafka.strimzi.io/v1beta2
kind: KafkaTopic
metadata:
  name: osprey-results
  namespace: osprey
  labels:
    strimzi.io/cluster: osprey-kafka
spec:
  partitions: 3
  replicas: 1
```

**Step 4:** Create overlays — production gets 3 Kafka replicas, 50Gi storage.

**Step 5:** Commit to divine-iac-coreconfig:
```bash
git commit -m "feat: add Strimzi Kafka operator and cluster for Osprey"
```

---

## Task 6: K8s Manifests — Osprey Deployments (Coordinator, Workers, UI)

**Repo:** `github.com/divinevideo/divine-iac-coreconfig`

**Files:**
- Create: `k8s/argocd/apps/osprey-coordinator.yaml`
- Create: `k8s/argocd/apps/osprey-workers.yaml`
- Create: `k8s/argocd/apps/osprey-ui.yaml`
- Create: `k8s/argocd/apps/osprey-etcd.yaml`
- Create: `k8s/argocd/apps/osprey-clickhouse-schema.yaml`
- Create: `k8s/applications/osprey-coordinator/base/deployment.yaml`
- Create: `k8s/applications/osprey-coordinator/base/service.yaml`
- Create: `k8s/applications/osprey-coordinator/base/kustomization.yaml`
- Create: `k8s/applications/osprey-workers/base/deployment.yaml`
- Create: `k8s/applications/osprey-workers/base/kustomization.yaml`
- Create: `k8s/applications/osprey-ui/base/deployment.yaml`
- Create: `k8s/applications/osprey-ui/base/service.yaml`
- Create: `k8s/applications/osprey-ui/base/httproute.yaml`
- Create: `k8s/applications/osprey-ui/base/kustomization.yaml`
- Create: `k8s/applications/osprey-etcd/base/statefulset.yaml`
- Create: `k8s/applications/osprey-etcd/base/service.yaml`
- Create: `k8s/applications/osprey-etcd/base/kustomization.yaml`
- Create: `k8s/applications/osprey-clickhouse-schema/base/init-schema-job.yaml`
- Create: `k8s/applications/osprey-clickhouse-schema/base/kustomization.yaml`
- Create overlays for poc/staging/production for each

**Step 1:** Create etcd StatefulSet (single node for poc, 3-node for production).

**Step 2:** Create Osprey coordinator deployment — Rust binary, needs etcd + Kafka config:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: osprey-coordinator
  namespace: osprey
spec:
  replicas: 1
  selector:
    matchLabels:
      app: osprey-coordinator
  template:
    spec:
      containers:
        - name: coordinator
          image: ghcr.io/divinevideo/osprey-coordinator:latest
          env:
            - name: ETCD_ENDPOINTS
              value: "http://osprey-etcd:2379"
            - name: KAFKA_BOOTSTRAP_SERVERS
              value: "osprey-kafka-kafka-bootstrap:9092"
```

**Step 3:** Create Osprey workers deployment — Python, needs Kafka + ClickHouse + Postgres config:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: osprey-workers
  namespace: osprey
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: worker
          image: ghcr.io/divinevideo/osprey-worker:latest
          env:
            - name: OSPREY_CLICKHOUSE_OUTPUT_SINK
              value: "true"
            - name: OSPREY_CLICKHOUSE_HOST
              value: "chi-funnelcake-clickhouse-0-0.funnelcake.svc"
            - name: OSPREY_CLICKHOUSE_DATABASE
              value: "osprey"
            - name: KAFKA_BOOTSTRAP_SERVERS
              value: "osprey-kafka-kafka-bootstrap:9092"
```

**Step 4:** Create Osprey UI deployment + HTTPRoute (expose via NGINX Gateway):

```yaml
# httproute.yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: osprey-ui
  namespace: osprey
spec:
  parentRefs:
    - name: nginx-gateway
      namespace: nginx-gateway
  hostnames:
    - "osprey.admin.divine.video"
  rules:
    - backendRefs:
        - name: osprey-ui
          port: 8080
```

**Step 5:** Create ClickHouse schema migration job (same pattern as funnelcake-clickhouse-schema):

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: osprey-clickhouse-schema-v1
spec:
  template:
    spec:
      containers:
        - name: migrate
          image: clickhouse/clickhouse-client:latest
          command: ["clickhouse-client", "--host", "$(CH_HOST)", "--multiquery"]
          args: ["--queries-file", "/schema/001_osprey_events.sql"]
```

**Step 6:** Create all overlays (poc = small resources, staging = medium, production = HA).

**Step 7:** Commit:
```bash
git commit -m "feat: add Osprey K8s deployments (coordinator, workers, UI, etcd, CH schema)"
```

---

## Task 7: Docker Images and CI

**Context:** Build Docker images for the Divine fork of Osprey (coordinator + worker) and push to GitHub Container Registry.

**Repo:** `github.com/divinevideo/osprey`

**Files:**
- Create: `.github/workflows/build-and-push.yaml`
- Modify: `osprey_coordinator/Dockerfile` (if needed for Divine config)
- Modify: `osprey_worker/Dockerfile` (add clickhouse-connect, divine plugins)

**Step 1:** Create GitHub Actions workflow — build on push to `divine/*` branches, push to `ghcr.io/divinevideo/osprey-coordinator` and `ghcr.io/divinevideo/osprey-worker`.

**Step 2:** Update worker Dockerfile to include Divine plugins:

```dockerfile
COPY divine/plugins /app/divine_plugins
ENV OSPREY_PLUGIN_PATH=/app/divine_plugins/src
```

**Step 3:** Commit:
```bash
git commit -m "ci: add Docker build workflow for Divine Osprey images"
```

---

## Task 8: Funnelcake → Kafka Bridge

**Context:** Funnelcake (the Nostr relay) needs to publish events to Kafka so Osprey can consume them. This is a lightweight sidecar or Funnelcake plugin.

**Files:**
- Create: `divine/nostr-kafka-bridge/main.py` — standalone Python service
- Create: `divine/nostr-kafka-bridge/Dockerfile`
- Create: `divine/nostr-kafka-bridge/requirements.txt`

**Step 1:** Create bridge service — connects to Funnelcake via WebSocket (wss://relay.divine.video), subscribes to all events, publishes to Kafka `nostr-events` topic:

```python
import asyncio
import json
from kafka import KafkaProducer
from websockets import connect

async def bridge(relay_url, kafka_servers, topic):
    producer = KafkaProducer(bootstrap_servers=kafka_servers)
    async with connect(relay_url) as ws:
        # Subscribe to all events
        await ws.send(json.dumps(["REQ", "bridge", {}]))
        async for msg in ws:
            data = json.loads(msg)
            if data[0] == "EVENT":
                event = data[2]
                producer.send(topic, json.dumps(event).encode())
```

**Step 2:** Dockerfile + K8s deployment manifest (add to divine-iac-coreconfig).

**Step 3:** Commit:
```bash
git commit -m "feat: add Nostr-to-Kafka bridge service"
```

---

## Summary — Task Dependencies

```
Task 1 (UI wiring)          — depends on: nothing (ClickHouse adapters done)
Task 2 (Divine plugins)     — depends on: nothing
Task 3 (SML models)         — depends on: nothing
Task 4 (SML rules)          — depends on: Task 2 (UDFs) + Task 3 (models)
Task 5 (K8s Kafka)          — depends on: nothing (separate repo)
Task 6 (K8s Osprey)         — depends on: Task 5 (needs Kafka cluster), Task 7 (needs images)
Task 7 (Docker/CI)          — depends on: Task 1 + Task 2
Task 8 (Kafka bridge)       — depends on: Task 5 (needs Kafka)
```

**Parallelizable groups:**
- **Group A:** Task 1 (UI wiring) — divinevideo/osprey
- **Group B:** Task 2 (plugins) + Task 3 (SML models) — divinevideo/osprey
- **Group C:** Task 5 (K8s Kafka) + Task 6 (K8s Osprey manifests, skeleton) — divinevideo/divine-iac-coreconfig
- **Group D (after A+B):** Task 4 (SML rules) — divinevideo/osprey
- **Group E (after A+B):** Task 7 (Docker/CI) — divinevideo/osprey
- **Group F (after C):** Task 8 (Kafka bridge) — divinevideo/osprey
