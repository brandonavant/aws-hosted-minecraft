# `infra/bootstrap/` — Terraform remote-state backend

This layer creates the S3 bucket and DynamoDB lock table that hold the **main** layer's remote state. It is its own
Terraform configuration with **no `backend` block**: bootstrap state lives on the operator's laptop. That solves the
chicken-and-egg problem — the layer creates the very bucket the main layer would otherwise need before `terraform
init` could even run.

This layer changes very rarely. Re-running it after the initial apply is unusual; recovery from a lost laptop is the
common reason (
see [Recovery: rebuild local state from live AWS resources](#recovery-rebuild-local-state-from-live-aws-resources)).

## Operator vs. agent split

| Command                           | Owner    | Notes                                                  |
|-----------------------------------|----------|--------------------------------------------------------|
| First `terraform init`            | Operator | Operator runs this so they can see how AWS init works. |
| Subsequent `terraform init`       | Agent    | Re-init after lock-file or backend-config edits.       |
| `terraform validate` / `fmt`      | Agent    | Routine authoring.                                     |
| `terraform plan`                  | Agent    | Required before any apply.                             |
| `terraform import`                | Agent    | n/a here — bootstrap has no imported resources.        |
| `terraform state rm` / `state mv` | Agent    | For fixing botched imports.                            |
| `terraform apply` / `destroy`     | Operator | Cloud-resource mutation; never run by the agent.       |

The agent will stop at every operator-owned step.

## First-run workflow

Prerequisites:

- AWS CLI v2.22+ on `PATH` and `MC_AWS_PROFILE` populated in `.env` (see `docs/aws-auth-setup.md`).
- `./scripts/aws/verify-credentials.sh` exits clean.
- Terraform 1.5.0+ on `PATH`.

Steps:

1. **Operator** — copy the tfvars template and pick a globally unique S3 bucket name:

    ```bash
    cd infra/bootstrap
    cp terraform.auto.tfvars.example terraform.auto.tfvars
    $EDITOR terraform.auto.tfvars   # set state_bucket_name to e.g. mc-aws-tfstate-<short-random>
    ```

   The `terraform.auto.tfvars` file is gitignored. The same `state_bucket_name` value will be re-used in
   `infra/backend-configs/prod.hcl` for the main layer.

2. **Operator** — run the first `terraform init`:

    ```bash
    terraform init
    ```

3. **Agent** — runs `terraform plan` and confirms the plan creates exactly one S3 bucket (with versioning, public
   access blocked, AES256 encryption) and one DynamoDB table (with `LockID` partition key) and nothing else.

4. **Operator** — runs `terraform apply` after reviewing the plan.

5. **Operator** (or agent on their behalf) — copies `infra/backend-configs/prod.hcl.example` to
   `infra/backend-configs/prod.hcl` (gitignored) and fills in the three outputs above. See `infra/README.md` for the
   first-run workflow that follows.

## What gets created

- **S3 bucket** holding `terraform.tfstate` for the main layer:
    - Versioning enabled (so a bad apply can be rolled back to a prior state object).
    - Public access fully blocked (ACLs, bucket policies, both).
    - Server-side encryption: SSE-S3 (AES256). KMS is a future option if a CMK ever lands in the project.
- **DynamoDB table** for state locks:
    - `PAY_PER_REQUEST` billing (cheapest at low volume).
    - `LockID` partition key (string), as required by the S3 backend.

## What is NOT created here

- Resources for the running Minecraft server. Those live in the main `infra/` layer.
- IAM users, roles, or permission sets. Operator access is via IAM Identity Center (see
  `docs/aws-auth-setup.md`).
- KMS keys. The state bucket uses SSE-S3, not SSE-KMS. Add KMS if/when a real reason emerges.

## Recovery: rebuild local state from live AWS resources

If `terraform.tfstate` for this layer is lost (laptop wiped, accidental `rm`, fresh clone of the repo on a new
machine), the live AWS resources still exist — they just no longer have a Terraform record. Reconstruct local state
**without using the AWS console**:

1. Re-create the working tree:

    ```bash
    cd infra/bootstrap
    cp terraform.auto.tfvars.example terraform.auto.tfvars
    $EDITOR terraform.auto.tfvars   # set state_bucket_name to the existing bucket's name
    terraform init
    ```

2. Discover the live resource names with the AWS CLI (uses `MC_AWS_PROFILE` from `.env`):

    ```bash
    aws s3api list-buckets \
      --profile "$MC_AWS_PROFILE" \
      --query 'Buckets[?contains(Name, `tfstate`)].Name' \
      --output text

    aws dynamodb list-tables \
      --profile "$MC_AWS_PROFILE" \
      --region "$(aws configure get region --profile "$MC_AWS_PROFILE")" \
      --query 'TableNames[?contains(@, `tfstate-locks`)]' \
      --output text
    ```

3. Import each resource. The bucket import key is the bucket name; the table import key is the table name:

    ```bash
    terraform import aws_s3_bucket.tfstate <bucket-name>
    terraform import aws_s3_bucket_versioning.tfstate <bucket-name>
    terraform import aws_s3_bucket_public_access_block.tfstate <bucket-name>
    terraform import aws_s3_bucket_server_side_encryption_configuration.tfstate <bucket-name>
    terraform import aws_dynamodb_table.tfstate_locks <table-name>
    ```

4. Run `terraform plan`. It must report **zero changes**. If it does not, the live resource has drifted from the
   committed `.tf` files; reconcile by adjusting the `.tf` (preferred) or planning a one-shot apply (operator).

## Local state hygiene

- `.terraform/` (provider plugins, modules cache) — gitignored.
- `terraform.tfstate*` — gitignored (state files contain resource metadata and may contain secrets).
- `terraform.auto.tfvars` (real values) — gitignored. Only `terraform.auto.tfvars.example` is committed.
- `.terraform.lock.hcl` — committed. Pinning provider hashes is a security and reproducibility concern.
