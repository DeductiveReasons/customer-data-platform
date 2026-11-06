# Production Architecture

November 6, 2026

This is the target production design for the customer data platform: every service, every model, and how data moves between them. It builds on the prototype in this repository ([architecture](ARCHITECTURE.md)) and the [production requirements](PRODUCTION_REQUIREMENTS.md). The access and accounts IT must provision for it are in [IT access request](IT_ACCESS_REQUEST.md).

Models are drawn as hexagons in every diagram, so you can follow where each one is trained, approved, served and monitored.

## The models

| # | Model | What it decides | Prototype | Production |
|---|---|---|---|---|
| M1 | Registration propensity | Chance a person registers if sent a given asset on a given channel (email or direct mail is a feature) | One logistic regression per asset, `src/cdp/nba_model.json`, retrain job `nba_retrain.py` | SageMaker training job (logistic regression first; LightGBM only if it beats it on real outcomes), versioned in SageMaker Model Registry, human approval before publish |
| M2 | Image theme selector | Which image theme is most likely to get a click | Contextual bandit, linear Thompson sampling, one arm per theme, 10% random holdout (`src/cdp/bandit.py`, `content.py`) | Same algorithm, updated daily from real click outcomes by a bandit update job, with an exploration cap and a manual off switch |
| M3 | Decision policy | Ranks options: `propensity x registration value - channel cost`, then contact rules, consent, approvals and experiment group | Code in `nba_decide.py`, `rules.py` | Same, with business values from finance in a versioned config, and every decision logged with model versions and scores |
| M4 | Send time | Best day and hour to send | Fixed rule (not learned) | Phase 5: learned model trained in SageMaker; until then the rule, enforced by the send queue |
| M5 | Image generation (offline) | Creates library images for new themes | Bedrock Stable Image Core in `us-west-2` (`scripts/generate_ai_images.py`) | Same Bedrock model, run on request; every image goes through content review before it can be sent |

M1, M2 and M4 run inside the decide service as small in-process models (JSON weights loaded from the approved model artifact in S3). If a future model is too large for Lambda, it moves to a SageMaker Serverless Inference endpoint without changing the rest of the flow.

## 1. End-to-end flow

```mermaid
flowchart TD
    classDef model fill:#fde7ea,stroke:#c8102e,stroke-width:2px,color:#111
    classDef store fill:#eef4ff,stroke:#3559a8,color:#111
    classDef ext fill:#f4f4f4,stroke:#777,color:#111

    subgraph SRC[Sources]
        S1[Web and app forms]:::ext
        S2[CRM and warehouse jobs]:::ext
        S3[Employer and eligibility files]:::ext
    end

    subgraph INTAKE[Step 1: Intake]
        I1[Route 53 + ACM<br/>api.example.com]
        I2[AWS WAF]
        I3[API Gateway<br/>usage plan per source]
        I4[Validate Lambda<br/>schema + event_id]
        I5[S3 landing<br/>Transfer Family SFTP optional]
        I6[EventBridge bus<br/>+ 30 day archive]
        I7[SQS + DLQ]
    end

    subgraph RECORD[Step 2: Customer record]
        R1[Step Functions<br/>identity resolution]
        R2[(DynamoDB profiles<br/>GSI next_decision_date)]:::store
        R3[(DynamoDB consent ledger<br/>append-only)]:::store
        R4[(Suppression list)]:::store
        R5[Merge review queue]
    end

    subgraph LAKE[Step 3: Data lake and features]
        L1[(S3 raw zone<br/>KMS)]:::store
        L2[Glue jobs<br/>curate + features]
        L3[(S3 curated + feature snapshots)]:::store
        L4[Glue Catalog + Lake Formation]
        L5[Athena workgroups]
    end

    subgraph ML[Step 4: Model training and registry]
        T1[EventBridge Scheduler<br/>weekly retrain]
        T2{{M1 Propensity training<br/>SageMaker training job}}:::model
        T3{{M4 Send-time training<br/>SageMaker, phase 5}}:::model
        T4[Evaluate vs current model<br/>and holdout]
        T5[(SageMaker Model Registry)]:::store
        T6[Human approval<br/>data science + compliance]
        T7[(S3 approved model artifacts<br/>versioned, KMS)]:::store
        T8{{M2 Bandit update job<br/>daily from clicks}}:::model
    end

    subgraph DECIDE[Step 5: Decisioning]
        D1[EventBridge Scheduler<br/>daily selection]
        D2[Selection Lambda<br/>query who is due]
        D3[SQS decision queue<br/>IDs only + DLQ]
        D4{{Decide Lambda<br/>M1 propensity + M2 image bandit<br/>+ M3 policy + M4 send time}}:::model
        D5[(Decision log<br/>DynamoDB + S3)]:::store
        D6[(Content and approvals<br/>DynamoDB + S3)]:::store
        D7[Experiment assignment<br/>no-contact holdout]
    end

    subgraph SEND[Step 6: Delivery]
        Q1[SQS email queue<br/>delay to send time]
        Q2[SQS mail queue]
        W1[Email worker<br/>final consent check<br/>idempotency key]
        W2[Mail worker<br/>address validation]
        V1[Braze or Amazon SES]:::ext
        V2[Print and mail vendor API]:::ext
    end

    subgraph OUT[Step 7: Outcomes]
        O1[API Gateway + WAF<br/>vendor webhooks]
        O2[Verify signature Lambda]
        O3[SQS feedback + DLQ]
        O4[Feedback Lambda]
        O5[Product registrations<br/>tracked link / QR]:::ext
    end

    subgraph CON[Step 8: Console]
        C1[CloudFront + S3 app]
        C2[Cognito<br/>federated to company SSO]
        C3[API Gateway JWT + Lambda]
        C4[Content review<br/>+ M5 image generation]
        C5{{M5 Bedrock<br/>Stable Image Core}}:::model
    end

    S1 --> I1 --> I2 --> I3 --> I4 --> I6
    S2 --> I1
    S3 --> I5 --> I6
    I6 --> I7 --> R1
    I6 --> L1
    R1 <--> R2
    R1 --> R5
    R3 --> R1
    R2 --> L1
    L1 --> L2 --> L3 --> L4 --> L5

    T1 --> T2
    T1 --> T3
    L3 --> T2
    L3 --> T3
    T2 --> T4
    T3 --> T4
    T4 --> T5 --> T6 --> T7
    L3 --> T8 --> T7

    D1 --> D2
    R2 --> D2 --> D3 --> D4
    T7 -->|approved models| D4
    D6 -->|approved content only| D4
    R3 --> D4
    R4 --> D4
    D7 --> D4
    D4 --> D5
    D5 --> L1
    D4 -->|email| Q1 --> W1 --> V1
    D4 -->|direct mail| Q2 --> W2 --> V2
    R4 --> W1
    R4 --> W2

    V1 --> O1
    V2 --> O1
    O5 --> O1
    O1 --> O2 --> O3 --> O4
    O4 --> R2
    O4 --> R3
    O4 --> R4
    O4 --> L1
    O4 -->|next decision| D3

    C1 --> C2
    C1 --> C3
    C3 --> L5
    C3 --> D6
    C3 --> T6
    C4 --> C5 --> D6
    C3 --> C4
```

How to read it:

1. **Intake** accepts events through a custom-domain API behind WAF, or bulk files through S3. Every event lands on the EventBridge bus and is archived to the raw zone.
2. **Customer record** resolves identity in Step Functions, keeps one profile per person, and holds consent and suppression separately. No consent record means no contact.
3. **Data lake** curates raw data and writes feature snapshots that both training and analytics use, so training sees exactly what was known at decision time.
4. **Training** retrains M1 (and later M4) on a schedule, compares each new model to the current one and to the holdout, registers it, and publishes only after a person approves. M2 updates daily from clicks.
5. **Decisioning** picks who is due each day, loads only approved models and approved content, and logs every decision with model versions, scores and experiment group.
6. **Delivery** uses one queue per channel. Workers recheck consent and suppression right before sending and pass an idempotency key so a retry never sends twice.
7. **Outcomes** come back as signed vendor webhooks and product registrations, update the profile and consent, feed the lake, and queue the next decision.
8. **Console** is behind company SSO. It shows results, approves content and models, and can generate new images with Bedrock, which then wait for review.

## 2. Model lifecycle

```mermaid
flowchart TD
    classDef model fill:#fde7ea,stroke:#c8102e,stroke-width:2px,color:#111
    classDef store fill:#eef4ff,stroke:#3559a8,color:#111
    classDef gate fill:#fff6d6,stroke:#b38600,color:#111

    A[(Decision log<br/>features, scores, model version,<br/>experiment group, probability)]:::store
    B[(Outcomes<br/>sent, delivered, opened, clicked,<br/>registered, unsubscribed)]:::store
    C[Glue join: decision + outcome<br/>attribution window]
    D[(Training snapshot S3<br/>point-in-time features)]:::store
    E{{M1 training<br/>SageMaker training job<br/>logistic regression / LightGBM}}:::model
    F{{M2 bandit update<br/>Thompson sampling posterior}}:::model
    G{{M4 send-time training<br/>phase 5}}:::model
    H[Offline evaluation<br/>AUC, calibration, lift vs holdout,<br/>fairness checks on approved features]
    I{Beats current model?}:::gate
    J[(SageMaker Model Registry<br/>PendingManualApproval)]:::store
    K{Data science + compliance<br/>approve?}:::gate
    L[(S3 approved artifact<br/>models/nba/vN.json, KMS)]:::store
    M{{Decide Lambda<br/>loads approved version}}:::model
    N[CloudWatch model monitoring<br/>input drift, score distribution,<br/>send-volume spikes]
    O[Rollback: point decide service<br/>at previous approved version]

    A --> C
    B --> C
    C --> D
    D --> E --> H
    D --> G --> H
    D --> F
    H --> I
    I -- no --> X[Keep current model]
    I -- yes --> J --> K
    K -- no --> X
    K -- yes --> L --> M
    F -->|daily, capped exploration,<br/>random holdout kept| L
    M --> A
    M --> N
    N -->|alarm| O
    O --> L
```

Rules that apply to every model:

- Features are reviewed by legal and data science before use. Age and employer attributes stay out until approved.
- A model is published only if it beats the current one on a holdout and a person approves it in the Model Registry.
- Every decision records the model version, so any result can be traced and any version rolled back.
- The no-contact holdout and the bandit's random holdout are never turned off, so lift is always measured against a fair comparison.

## 3. One decision, step by step

```mermaid
sequenceDiagram
    autonumber
    participant Sch as EventBridge Scheduler
    participant Sel as Selection Lambda
    participant Q as SQS decision queue
    participant Dec as Decide Lambda (M1-M4)
    participant Prof as DynamoDB profile, consent, suppression
    participant Mod as S3 approved models
    participant Con as Content approvals
    participant Log as Decision log
    participant SQ as SQS email or mail queue
    participant W as Send worker
    participant V as Email or mail vendor
    participant FB as Feedback Lambda

    Sch->>Sel: daily run
    Sel->>Prof: query GSI next_decision_date <= today
    Sel->>Q: customer IDs only
    Q->>Dec: one ID per message
    Dec->>Prof: read profile, consent, suppression
    Dec->>Mod: load approved M1, M2, M4 (cached per container)
    Dec->>Con: approved, unexpired variations
    Note over Dec: assign experiment group (holdout?)<br/>M1 scores each asset x channel<br/>M3 ranks value - cost, applies contact rules<br/>M2 picks image theme, then image<br/>M4 sets send time
    Dec->>Log: decision, scores, model versions, probability
    alt no contact (holdout, no consent, journey complete)
        Dec->>Prof: set next_decision_date
    else email or direct mail
        Dec->>SQ: message ID, delay until send time
        SQ->>W: deliver at send time
        W->>Prof: recheck consent and suppression
        W->>V: send with idempotency key
        V-->>FB: signed webhook (delivered, opened, clicked, unsubscribed)
        FB->>Prof: update profile, consent, suppression
        FB->>Q: queue next decision when due
    end
```

## 4. Accounts and deployment

```mermaid
flowchart TB
    classDef acct fill:#f7f7f7,stroke:#444,color:#111

    subgraph ORG[AWS Organization: Control Tower or company landing zone]
        MGMT[Management account<br/>SCPs, Identity Center, billing]:::acct
        SEC[Security account<br/>GuardDuty, Security Hub, Config,<br/>Macie, Access Analyzer admin]:::acct
        LOG[Log archive account<br/>CloudTrail org trail, Config history]:::acct
        SHR[Shared services account<br/>Terraform state, artifact bucket]:::acct
        DEV[cdp-dev<br/>synthetic data only]:::acct
        STG[cdp-staging<br/>synthetic data, load tests]:::acct
        PRD[cdp-prod<br/>real data]:::acct
    end

    GH[GitHub repository<br/>customer-data-platform] -->|Actions OIDC| CI[GitHub Actions<br/>test, scan, terraform plan]
    CI -->|assume cdp-deploy-dev| DEV
    CI -->|assume cdp-deploy-staging| STG
    CI -->|assume cdp-deploy-prod<br/>after environment approval| PRD
    CI --> SHR
    PEOPLE[Engineers] -->|Identity Center SSO| DEV
    PEOPLE -->|read-only, break-glass for changes| PRD
    PRD -->|AWS Backup cross-region copies| DR[(Second region)]
    DEV --> LOG
    STG --> LOG
    PRD --> LOG
```

- Infrastructure stays in Terraform. State moves from the prototype's local file to an encrypted S3 bucket with locking in the shared services account, one state per environment.
- Engineers sign in through IAM Identity Center. Production changes go only through the CI deploy role after an approval in the GitHub `prod` environment.
- No VPC is needed for the serverless core. Add one, with VPC endpoints, only if the warehouse or CRM must be reached privately or company policy requires egress control.

## 5. Services by component

| Component | AWS services | Notes |
|---|---|---|
| Edge | Route 53, ACM, CloudFront, AWS WAF | Custom domains for the API and console; ACM certificates for CloudFront must be in `us-east-1` |
| Intake | API Gateway (REST), Lambda, EventBridge, SQS, S3, Transfer Family (optional) | Usage plan and key per source system; event_id de-duplication |
| Customer record | Step Functions, DynamoDB | Profiles with a GSI on next decision date; append-only consent ledger; suppression list; point-in-time recovery |
| Data lake | S3 (raw, curated, restricted), Glue jobs and Data Catalog, Lake Formation, Athena | Query limits per workgroup; restricted zone for contact details |
| Models | SageMaker (training jobs, Model Registry, optional Serverless Inference), ECR (training images), Bedrock (Stable Image Core, `us-west-2`) | Weekly M1 retrain, daily M2 update, human approval in the registry |
| Decisioning | EventBridge Scheduler, Lambda, SQS, DynamoDB | IDs only in queues; reserved concurrency on the decide function |
| Delivery | SQS, Lambda, Amazon SES (if not Braze), Secrets Manager for vendor keys | Delay queues enforce send time; idempotency keys on every send |
| Outcomes | API Gateway, WAF, Lambda, SQS | Vendor webhook signatures verified before anything is stored |
| Console | CloudFront, S3, Cognito (federated SSO), API Gateway JWT authorizer, Lambda | Roles mask personal data; every profile view is logged |
| Security | KMS customer-managed keys, Secrets Manager rotation, CloudTrail, GuardDuty, Security Hub, Config, Macie, IAM Access Analyzer | Separate KMS key per data class |
| Operations | CloudWatch (logs, metrics, alarms, dashboards), X-Ray, SNS to the on-call tool, AWS Budgets, Cost Explorer | Alarm on every DLQ, failed run, consent-check failure and send-volume spike |
| Recovery | AWS Backup | Cross-region copies of DynamoDB and critical S3 data; RPO 1 hour, RTO 4 hours |
| Delivery pipeline | GitHub Actions, IAM OIDC provider, S3 state bucket with locking | Plan on every pull request, apply with approval |

## 6. Data protection

| Data | Where it lives | Protection |
|---|---|---|
| Contact details (name, email, address) | DynamoDB profiles, S3 restricted zone | KMS key for personal data; never in queue messages, logs or partition keys; masked in the console by role |
| Consent and suppression | DynamoDB ledger, S3 | Append-only, dated, with source and legal text version |
| Decisions and outcomes | DynamoDB, S3 curated | Customer ID only; used for training and reporting |
| Model artifacts | S3 approved artifacts, SageMaker Model Registry | Versioned; publish requires approval; rollback by version |
| Vendor credentials | Secrets Manager | Rotated; read only by the send workers |
| Images and content | S3, approvals table | AI-generated images reviewed before approval |

## 7. Moving from the prototype

| Prototype | Production change |
|---|---|
| Local Terraform state with secrets | Remote encrypted state, OIDC deploys, secrets created outside state where possible |
| Full table scan to find who is due | GSI query on next decision date |
| Queues carry traits and contact details | Queues carry IDs only |
| Shared console password | Cognito with company SSO and roles |
| Simulated outcomes | Signed vendor webhooks and real registrations |
| No approval record means approved | No approval record means not sendable |
| Model retrained in Lambda without a gate | SageMaker training, evaluation, registry and human approval |
| Bandit optimizes clicks | Confirm with real data that clicks predict registrations |
| AWS-managed encryption keys | Customer-managed KMS keys per data class |
| One account, no guardrails | Dev, staging and prod accounts under the company organization |

AWS service details here come from general knowledge and were not checked against current AWS pages. Confirm them with your AWS account team.
