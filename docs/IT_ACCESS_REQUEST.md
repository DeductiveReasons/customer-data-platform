# IT Access Request

November 6, 2026

Everything IT needs to provision so this customer data platform can be built in the company environment: accounts, sign-in, permissions, service approvals, network access, GitHub and workstation tools. Part A is what the current code needs to deploy as a proof of concept. Part B adds what the [production architecture](PRODUCTION_ARCHITECTURE.md) needs.

Send IT the [checklist](#checklist-to-send-it) at the end. No secrets should travel by email or chat: share any keys through the company password manager, and they are stored only in AWS Secrets Manager.

## Part A: Proof of concept (what this repository deploys today)

### A1. AWS account

| Request | Detail |
|---|---|
| Account | One sandbox AWS account in the company organization, for example `cdp-poc` (later `cdp-dev`) |
| Data | Synthetic data only. The code generates fake people; no company or customer data is needed |
| Budget | AWS Budgets alert at a monthly amount you choose, sent to the team. The daily Step Functions run is on by default and costs a little every day |
| Regions allowed by SCP | `us-east-2` (where everything deploys), `us-east-1` (Cost Explorer API is called there), `us-west-2` (only if generating images with Bedrock) |
| Tags | Resources are tagged `Project=cdp` and `ManagedBy=terraform` |

### A2. Sign-in (IAM Identity Center)

| Request | Detail |
|---|---|
| User | Your company identity in IAM Identity Center, assigned to the sandbox account. No IAM users or access keys |
| Permission set | `cdp-builder`: AdministratorAccess is simplest for a sandbox. If a scoped set is required, use the actions in [A3](#a3-permissions-the-deployer-needs) |
| Session length | 8 to 12 hours. The year-simulation script runs for close to an hour and refreshes credentials from the SSO profile |
| CLI | AWS CLI v2, recent enough to include `aws login` |

How to connect once the account and permission set exist:

```bash
aws configure sso --profile cdp
# SSO start URL: from IT, SSO region: from IT, account: cdp-poc, role: cdp-builder, default region: us-east-2
aws login --profile cdp            # each session (or: aws sso login --profile cdp)
export AWS_PROFILE=cdp
aws sts get-caller-identity        # confirms you are in the right account
```

### A3. Permissions the deployer needs

Terraform creates these resources, so the permission set must allow creating, updating, tagging and deleting them:

| Service | Resources Terraform creates | Actions |
|---|---|---|
| IAM | 19 roles named `cdp-*` with inline policies (13 Lambda roles, 2 Step Functions roles, 1 EventBridge role, export, receiver and web app roles) | `iam:CreateRole`, `DeleteRole`, `GetRole`, `TagRole`, `UpdateAssumeRolePolicy`, `PutRolePolicy`, `GetRolePolicy`, `DeleteRolePolicy`, `ListRolePolicies`, `ListAttachedRolePolicies`, `ListInstanceProfilesForRole`, and `iam:PassRole` to `lambda.amazonaws.com`, `states.amazonaws.com`, `events.amazonaws.com` |
| Lambda | 16 functions (Python 3.12, arm64), permissions for API Gateway and EventBridge | `lambda:*` on `cdp-*` |
| API Gateway | REST API `cdp` with API key and usage plan; HTTP APIs `cdp-receiver` and `cdp-webapp` | `apigateway:*` |
| DynamoDB | Tables `cdp-profiles`, `cdp-receiver-messages`, `cdp-variation-approvals` (TTL, point-in-time recovery) | `dynamodb:*` on `cdp-*` |
| S3 | One bucket `cdp-raw-<account-id>` (versioning, lifecycle, block public access, TLS-only policy) | `s3:*` on `cdp-*` buckets |
| SQS | 16 queues including dead-letter queues | `sqs:*` on `cdp-*` |
| EventBridge | Bus `cdp`, 30-day archive, 6 rules | `events:*` |
| Step Functions | `cdp-pipeline`, `cdp-daily` | `states:*` on `cdp-*` |
| Secrets Manager | `cdp/braze`, `cdp/crm`, `cdp/other_apis`, `cdp/receiver-token`, `cdp/webapp-password` | `secretsmanager:*` on `cdp/*` |
| Glue | Database `cdp_analytics`, 4 tables | `glue:*` on the `cdp_analytics` database and its tables |
| Athena | Workgroup `cdp`, 6 saved queries | `athena:*` on the `cdp` workgroup |
| CloudWatch and Logs | Log groups, alarms, dashboard `cdp-nba`, metrics namespace `cdp/NBA` | `logs:*`, `cloudwatch:*` |
| SNS | Topic `cdp-alerts`, optional email subscription | `sns:*` on `cdp-*` |
| Cost Explorer | Read by the web app's Usage and costs page | `ce:GetCostAndUsage` (Cost Explorer must be enabled for the account) |
| Bedrock (optional) | No resources; the image script calls the model | `bedrock:InvokeModel` on `stability.stable-image-core-v1:1` in `us-west-2`, plus `aws-marketplace:Subscribe` and `aws-marketplace:ViewSubscriptions` for the one-time model subscription |
| STS | Caller identity | `sts:GetCallerIdentity` |

Things that commonly need an exception in a company organization:

1. **Creating IAM roles.** The code does not set a permission boundary or role path. If the organization requires one, tell us its name and path; we add `permissions_boundary` and `path` to each role.
2. **Public API endpoints.** Three API Gateway endpoints are public: event intake (API key), the dummy receiver (bearer token) and the web app (shared password). There is no WAF, custom domain or IP allowlist yet. Ask whether they need WAF or an IP allowlist in the sandbox.
3. **Default encryption.** S3, DynamoDB, SQS and Secrets Manager use AWS-managed keys. If an SCP requires customer-managed KMS keys, we need a KMS key and `kms:*` on it, and the Terraform changes to use it.
4. **Bedrock model access.** Stability AI models need a Marketplace subscription, which some organizations block.
5. **Cost Explorer.** Often limited to the payer account. If it can't be enabled, only the web app's costs page stops working.
6. **SNS email.** The alert topic can subscribe an email address. Some organizations block external addresses.

### A4. Terraform state

| Request | Detail |
|---|---|
| State bucket | S3 bucket, for example `cdp-tfstate-<account-id>`, versioned, encrypted, public access blocked |
| Locking | S3 native locking (Terraform 1.10+) or a DynamoDB table `cdp-tflock` with partition key `LockID` |
| Access | Read/write for the `cdp-builder` permission set |

Today the code keeps state in a local file that contains the generated receiver token and web app password. With the bucket in place we add a `backend "s3"` block, so state never sits on a laptop.

### A5. Network access from your workstation

| Destination | Why |
|---|---|
| `*.amazonaws.com`, the company SSO portal | AWS CLI, Terraform, SSO sign-in |
| `registry.terraform.io`, `releases.hashicorp.com` | Terraform providers: hashicorp/aws ~> 5.0, archive ~> 2.4, random ~> 3.6 |
| `pypi.org`, `files.pythonhosted.org` | Python packages for model training: lightgbm 4.6.0, numpy 2.2.6, scikit-learn 1.7.2, boto3, and optionally Pillow |
| `github.com` | Source code |

If the company uses a TLS-inspecting proxy, provide its root certificate for Python, Terraform and the AWS CLI.

Lambda functions run outside a VPC and make outbound HTTPS calls only if a destination (Braze, CRM or webhook) is turned on. They are off by default.

### A6. GitHub

| Request | Detail |
|---|---|
| Repository | Fork or transfer `customer-data-platform` into the company GitHub organization, private |
| Access | Write access for you; branch protection on `main` |
| Pages | Optional. The repository publishes a read-only demo snapshot with GitHub Pages |
| Sign-in | GitHub account under company SSO with 2FA, and `gh auth login` on your workstation |

### A7. Workstation

| Tool | Version |
|---|---|
| Python | 3.12 |
| Terraform | 1.6 or later (1.10+ if using S3 native locking) |
| AWS CLI | v2, recent version with `aws login` and `aws configure export-credentials` |
| Git | Current |
| Shell | Bash. On Windows use Git Bash or WSL, because the README commands use `export` and `$(...)` |
| Rights | Permission to install the tools above and Python packages |

### A8. Optional third-party destinations

Only needed if you turn on a destination. Each value goes into Secrets Manager, never into code.

| Destination | Secret | Keys |
|---|---|---|
| Braze | `cdp/braze` | `rest_endpoint`, `api_key` (a Braze REST API key with user track permissions) |
| CRM webhook | `cdp/crm` | `url`, `token` |
| Other APIs | `cdp/other_apis` | `url`, `token` |

## Part B: Production

Everything in Part A, plus the following. The design is in [production architecture](PRODUCTION_ARCHITECTURE.md), and the reasons are in [production requirements](PRODUCTION_REQUIREMENTS.md).

### B1. Accounts and agreements

| Request | Detail |
|---|---|
| Workload accounts | `cdp-dev`, `cdp-staging`, `cdp-prod`. Real customer data only in `cdp-prod` |
| Shared accounts | Log archive, security tooling and shared services (Terraform state, build artifacts), if the company doesn't already have them |
| Guardrails | Control Tower or the company landing zone. SCPs must allow every service below and restrict regions to `us-east-2`, `us-east-1` and `us-west-2` (Bedrock), plus the backup region |
| Business Associate Addendum | Through AWS Artifact, if legal confirms the data is protected health information |
| Support | Business Support or higher on `cdp-prod` |
| Budgets | One per account, sent to the team |

### B2. Sign-in and roles

| Permission set or role | Who | Access |
|---|---|---|
| `cdp-builder` | Engineers | Full deploy rights in `cdp-dev` only |
| `cdp-readonly` | Engineers | Read-only in staging and prod, with no access to customer data tables or the restricted S3 zone |
| `cdp-breakglass` | On-call lead | Time-limited admin in prod, alerted and logged |
| `cdp-deploy-<env>` | GitHub Actions | IAM role trusted by the GitHub OIDC provider, limited to `repo:<org>/customer-data-platform` and the matching GitHub environment. Prod applies need approval |
| `cdp-data-science` | Data scientists | SageMaker, Athena and the curated S3 zone in dev and staging. Model approval in the prod Model Registry |

### B3. Additional AWS services

| Service | Used for | Approval or setup needed |
|---|---|---|
| KMS | Customer-managed keys per data class | Key policies naming the service roles |
| AWS WAF | In front of the intake API, webhooks and console | Managed rule groups and rate limits |
| Route 53 and ACM | Custom domains for the API and console | A hosted zone or a delegated subdomain, and DNS access |
| CloudFront and S3 | Hosting the console | None |
| Cognito | Console sign-in federated to company SSO | A SAML or OIDC application in the company identity provider |
| EventBridge Scheduler | Daily selection and weekly retraining | None |
| Glue jobs, Lake Formation | Curated and restricted data lake zones | Lake Formation admins named |
| SageMaker | Training jobs, Model Registry, optional serverless inference | Service quotas for the chosen training instance type |
| ECR | Training container images | None |
| Amazon Bedrock | Stable Image Core in `us-west-2` | Model access and Marketplace subscription |
| Amazon SES | Email, if Braze isn't used | Production access request, verified domain with DKIM, SPF and DMARC, dedicated IP decision |
| Transfer Family | SFTP for partner files, only if partners require it | A public or VPC endpoint decision |
| X-Ray | Tracing | None |
| AWS Backup | Cross-region copies of DynamoDB and critical S3 data | Backup vault in the second region |
| CloudTrail, GuardDuty, Security Hub, Config, Macie, IAM Access Analyzer | Security and audit | Usually enabled from the security account |
| Secrets Manager rotation | Vendor keys | Rotation functions, or a rotation process per vendor |

### B4. Vendors

| Vendor | What IT or the business must provide |
|---|---|
| Email platform (Braze or SES) | Account, REST API key with send and user permissions, authenticated sending domain, webhook signing key |
| Direct mail print vendor | Account, API key, address validation service, webhook signing key |
| On-call tool | An integration endpoint for SNS alerts, for example PagerDuty or Opsgenie |
| Company identity provider | SAML or OIDC app for the console (Cognito), and groups for console roles |

### B5. Network

No VPC is needed for the serverless core. Provision one only if the warehouse or CRM must be reached privately or policy requires controlled egress. Then add subnets, NAT or an egress proxy, VPC endpoints (S3, DynamoDB, SQS, Secrets Manager, KMS, CloudWatch Logs, Bedrock, SageMaker), and Transit Gateway or PrivateLink to company networks.

### B6. Quotas to raise before launch

- Lambda concurrent executions
- API Gateway requests per second
- SES sending quota and rate, if used
- Bedrock image requests per minute, if used
- SageMaker training instances for the chosen type

## Checklist to send IT

1. A sandbox AWS account (`cdp-poc` or `cdp-dev`) in the company organization, with a budget alert
2. SCPs allowing regions `us-east-2`, `us-east-1` and `us-west-2`, and the services in A3
3. My identity in IAM Identity Center with a `cdp-builder` permission set on that account (AdministratorAccess, or the scoped actions in A3), with 8 to 12 hour sessions
4. Permission to create IAM roles named `cdp-*` and pass them to Lambda, Step Functions and EventBridge, or the name of the permission boundary we must attach
5. Cost Explorer enabled for the account, and Bedrock access to Stability AI Stable Image Core in `us-west-2` (optional)
6. An S3 bucket and lock for Terraform state, encrypted and versioned
7. Workstation rights to install Python 3.12, Terraform 1.6+, AWS CLI v2, Git and Git Bash or WSL
8. Network access to `*.amazonaws.com`, `registry.terraform.io`, `releases.hashicorp.com`, `pypi.org`, `files.pythonhosted.org` and `github.com`, plus the proxy root certificate if there is one
9. A private repository in the company GitHub organization with write access
10. Confirmation that three public API Gateway endpoints are allowed in the sandbox, or the WAF or IP allowlist rules to apply
11. For production later: the accounts, roles, services, vendors and quotas in Part B

AWS service details here come from general knowledge and were not checked against current AWS pages. Confirm them with your AWS account team.
