# Customer Data Platform on AWS

An event-driven customer data pipeline in the style of Segment, Hightouch or RudderStack, built from managed AWS services and deployed with Terraform.

Failures in Braze, a CRM or any other destination never stop events from being received. Each destination has its own queue, retries and dead-letter queue.

```mermaid
flowchart TD
    A[Web and mobile] --> D
    B[CRM and APIs] --> D
    C[Data warehouse jobs] --> D
    D[API Gateway] --> E[Ingest Lambda: validate]
    E --> F[EventBridge]
    F --> G[Firehose] --> H[(S3: raw events)]
    F --> Q[EventBridge archive: replay]
    F --> I[SQS processing queue]
    I --> J[Starter Lambda]
    J --> K[Step Functions]
    K <--> L[(DynamoDB: profiles and identities)]
    K --> M[Rules: consent and routing]
    M --> N1[SQS] --> O1[Braze]
    M --> N2[SQS] --> O2[CRM webhook]
    M --> N3[SQS] --> O3[Other APIs webhook]
```

## Deploy

Requires Terraform 1.6+, the AWS CLI and AWS credentials. Deploying creates billable resources.

```sh
cd infra
terraform init
terraform plan
terraform apply
```

No destination receives data until you enable it. Add its credentials, then list it in `enabled_destinations`:

```sh
aws secretsmanager put-secret-value --secret-id cdp/braze \
  --secret-string '{"rest_endpoint":"https://rest.iad-01.braze.com","api_key":"..."}'
aws secretsmanager put-secret-value --secret-id cdp/crm \
  --secret-string '{"url":"https://example.com/hook","token":"..."}'

terraform apply -var='enabled_destinations=["braze","crm"]'
```

## Send events

```sh
API_KEY=$(aws apigateway get-api-key --include-value --api-key "$(terraform output -raw api_key_id)" --query value --output text)

curl -X POST "$(terraform output -raw events_url)" -H "x-api-key: $API_KEY" -d '{
  "type": "consent", "user_id": "u1", "consent": {"marketing": true}
}'

curl -X POST "$(terraform output -raw events_url)" -H "x-api-key: $API_KEY" -d '{
  "type": "track", "event": "Signed Up", "user_id": "u1", "traits": {"plan": "pro"}
}'
```

Events need a `type` (`track`, `identify`, `page`, `screen` or `consent`) and at least one of `user_id`, `anonymous_id` or `email`. Send up to 100 at once as `{"batch": [...]}`. Set your own `event_id` to make retries safe.

## How it behaves

- **Consent:** delivery is blocked without consent. Braze requires `marketing`, the CRM requires `crm`, and other APIs require `analytics`. Change this in `src/cdp/rules.py`.
- **Identity:** identifiers seen together link to one customer. Emails are stored in keys as SHA-256 hashes, although any email sent inside `traits` is stored as sent.
- **Duplicates:** an `event_id` is processed once for as long as Step Functions remembers the execution name (90 days).
- **Failures:** messages retry 5 times, then move to a dead-letter queue and raise an alarm.

## Known limitations

- Conflicting identities are logged and not merged. Merging needs a business rule.
- Replaying an event from the archive or S3 is a no-op while its execution name is remembered, so a replay within 90 days needs new `event_id` values.
- CRM and other API destinations are generic webhooks. Salesforce OAuth and field mapping are not included.
- The data warehouse path is a push to the batch API. Scheduled warehouse extraction is not included.
- The API key is suitable for servers. Browser use needs CORS and a public write-key approach.
- There is no deletion workflow for privacy requests, such as GDPR or CCPA erasure.
- CloudTrail is an account-level service and is not created here.
- Terraform state is local. Use a remote backend for team or production use.

## Next best action demo

This adds a synthetic population, a propensity model and a decision pipeline that calls a dummy email and direct mail service. **No real email or mail is ever sent.** The receiver only validates and stores requests for 7 days.

```mermaid
flowchart TD
    A[scripts/ingest_sample.py: 10,000 fake people] --> B[Ingestion API]
    B --> C[(DynamoDB profiles)]
    D[Schedule or manual run] --> E[Scan Lambda]
    C --> E
    E --> F[SQS decisions queue]
    F --> G[Decide Lambda: score, check consent, build HTML]
    G -->|POST /email| H[Dummy receiver]
    G -->|POST /direct-mail| H
    H --> I[(Stored requests)]
```

Run it after deploying:

```sh
cd infra
API_KEY=$(aws apigateway get-api-key --include-value --api-key "$(terraform output -raw api_key_id)" --query value --output text)
python ../scripts/ingest_sample.py "$(terraform output -raw events_url)" "$API_KEY"

# Wait for the events to finish processing, then run a scan.
aws lambda invoke --function-name "$(terraform output -raw nba_scan_function)" /dev/stdout

# See what the dummy service received.
aws dynamodb scan --table-name "$(terraform output -raw receiver_table)" --select COUNT
```

Set `nba_schedule_enabled=true` to scan daily. To retrain or preview without AWS:

```sh
python scripts/train_model.py
python scripts/simulate_local.py
```

**How decisions work:** one logistic regression per asset estimates the chance of registering, given the customer's traits, their engagement so far and the channel. The pipeline picks the asset and channel with the highest `propensity x registration value - channel cost`, and stops if none is positive. Contact needs `marketing` consent and consent for that channel. Each customer gets at most 4 sign-up messages, never the same asset twice in a row, and 10% of decisions explore other assets so the model keeps learning.

### Eric Health asset library

Eric Health is a fictional company used for this demo. `src/cdp/asset_library.json` holds the campaign aimed at fitness program sign-ups: a program introduction, a member story, a free trial, a coach session and an onboarding welcome. Each renders as HTML for email or direct mail. Run `python scripts/preview_assets.py` to create a local gallery.

### Feedback loop

```mermaid
flowchart TD
    A[Decide Lambda] -->|asset as HTML| B[Dummy receiver]
    B -->|simulated opened, clicked, registered, unsubscribed| C[SQS feedback queue]
    C --> D[Feedback Lambda]
    D -->|update counters and consent| E[(DynamoDB profile)]
    D -->|training rows| F[(S3 feedback)]
    D -->|request next decision| G[SQS decisions queue]
    G --> A
    F --> H[Retrain Lambda]
    H -->|publish if it passes checks| I[(S3 latest model)]
    I --> A
```

Feedback affects decisions in two ways. It changes the customer's next decision straight away, because their opens and clicks are model features. It also trains new models when the retrain Lambda runs, which only publishes assets with at least 500 rows, 20 registrations and a holdout AUC of 0.55. Registered customers get one onboarding message and then leave the sign-up journey. An unsubscribe removes consent for that channel.

Run a retrain after a campaign, allowing a few minutes for Firehose to deliver the data:

```sh
aws lambda invoke --function-name "$(terraform output -raw nba_retrain_function)" /dev/stdout
```

Limitations:

- The people, behavior, model accuracy, registration value and channel costs are all invented. The results show that the pipeline works, not how a real campaign would perform.
- The dummy receiver sends feedback through SQS. A real provider would call a signed webhook, and its events would arrive over hours or days.
- Direct mail reads cannot be observed, so mail only reports QR code visits.
- Scores are not causal uplift estimates, because a person who registers might have done so without the message. Measuring lift needs a holdout group that receives nothing.
- Exploration rows are not reweighted during training, and there is no drift monitoring or automated rollback.
- A person whose send is recorded but whose feedback never arrives stays paused until the next scan.
- A real sender would also need suppression lists, working unsubscribe links, address validation and legal review, especially for any health-related claims.

## Test

```sh
python -m unittest discover -s tests
```
