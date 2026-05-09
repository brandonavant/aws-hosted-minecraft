# `infra/` — main Terraform layer

This layer manages the AWS resources that back the live LightSail Minecraft deployment. It uses the S3 bucket and
DynamoDB table created by `infra/bootstrap/` for remote state and locking.

The configuration was reverse-engineered from a hand-built deployment (issue #4). The first apply imports existing
resources rather than creating new ones; after that, the layer is the source of truth.

## Operator vs. agent split

| Command                           | Owner    | Notes                                                  |
|-----------------------------------|----------|--------------------------------------------------------|
| First `terraform init`            | Operator | First-time AWS init goes by hand for visibility.       |
| Subsequent `terraform init`       | Agent    | Re-init after lock-file or backend-config edits.       |
| `terraform validate` / `fmt`      | Agent    | Routine authoring.                                     |
| `terraform plan`                  | Agent    | Required before any apply.                             |
| `terraform import`                | Agent    | State-only mutation; doesn't touch cloud.              |
| `terraform state rm` / `state mv` | Agent    | For fixing botched imports.                            |
| `terraform apply` / `destroy`     | Operator | Cloud-resource mutation; never run by the agent.       |

## First-run workflow

Prerequisites:

- The bootstrap layer is applied (`infra/bootstrap/`) and you have its outputs (`state_bucket_name`,
  `lock_table_name`, `region`) handy for the backend-config step below.
- `MC_AWS_PROFILE` is populated in `.env` and `./scripts/aws/verify-credentials.sh` exits clean.
- Terraform 1.5.0+ on `PATH`.

Steps:

1. **Operator** — copy the backend-config template and fill in your bootstrap layer's outputs:

    ```bash
    cd infra
    cp backend-configs/prod.hcl.example backend-configs/prod.hcl
    $EDITOR backend-configs/prod.hcl   # set bucket, dynamodb_table, profile
    ```

   The real `prod.hcl` is gitignored — its bucket / table / profile values are
   operator-specific and don't belong in a public repo.

2. **Operator** — initialize with the prod backend config:

    ```bash
    terraform init -backend-config=backend-configs/prod.hcl
    ```

3. **Agent** — runs `terraform import` once per existing live resource (see [Import map](#import-map)).
   After each import, the agent runs `terraform plan` to catch shape mismatches and adjusts the `.tf` files until
   plan is clean for that resource.

4. **Agent** — runs a final `terraform plan`. It must report **zero changes** for everything that was imported.
   The output is captured for the PR description.

5. **Operator** — runs `terraform apply` to materialize the resources that could not be imported (the
   `aws_lightsail_instance_public_ports.mcserver` resource — see
   [Resources that can't be imported](#resources-that-cant-be-imported)). The apply is a no-op against AWS for
   firewall rules, since the desired ports already match the live state, but it adds the resource to Terraform
   state.

6. **Agent** — runs one more `terraform plan`. From this point on, plan should report zero changes.

## Import map

Each line is the `terraform import` command for an existing live resource:

```bash
terraform import aws_lightsail_instance.mcserver               mcserver-prod
terraform import aws_lightsail_disk.mcserver_data              disk-mcserver-prod
terraform import aws_lightsail_disk_attachment.mcserver_data   'disk-mcserver-prod,mcserver-prod'
terraform import aws_lightsail_static_ip.mcserver              ip-mcserver-prod
terraform import aws_lightsail_static_ip_attachment.mcserver   ip-mcserver-prod
terraform import aws_route53_record.mc                         'Z09024551TI8L9018Y7IE_mc.bytehorizonforge.com_A'
```

The `bytehorizonforge.com` Route 53 zone is **not** imported — it is read via a `data` source instead. See
[Why the bytehorizonforge zone is a data source](#why-the-bytehorizonforge-zone-is-a-data-source).

## Resources that can't be imported

Three categories of resources exist in the live deployment but aren't (or can't be) brought into Terraform import
state. Each is handled deliberately:

- **`aws_lightsail_instance_public_ports.mcserver`** — the AWS provider does not implement `ImportState` for this
  resource (verified against the v6.44.0 source). It is declared in `lightsail.tf` matching the live firewall rules
  byte-for-byte, so the first `terraform apply` issues a no-op `PutInstancePublicPorts` API call and adds the
  resource to state without changing AWS-side behavior.
- **The `ssh-mcserver` Lightsail key pair** — the AWS provider documentation explicitly says: "You cannot import
  Lightsail Key Pairs because the private and public key are only available on initial creation." The instance
  references the key pair by name (`key_pair_name = "ssh-mcserver"`), but the key pair itself stays out of Terraform
  state. The PEM file the operator already holds is the only copy.
- **LightSail instance snapshots** — the AWS provider (v6.44.0) does not have an
  `aws_lightsail_instance_snapshot` resource at all. Existing snapshots live in AWS independently of Terraform; the
  AutoSnapshot add-on on the instance creates new ones daily as configured (`snapshot_time = "11:00"` UTC).

## Why the bytehorizonforge zone is a data source

The `bytehorizonforge.com.` zone pre-existed this project and hosts records for unrelated concerns — specifically
an Azure M365 email setup at `auth.bytehorizonforge.com.` (MX, TXT, two DKIM CNAMEs). The Minecraft Terraform does
**not** own that zone; it merely places one record (`mc.bytehorizonforge.com.`) inside it.

Expressing that with a `data "aws_route53_zone"` block instead of `resource "aws_route53_zone"` keeps the ownership
boundary truthful: Terraform looks up the zone, but cannot create it, modify it, or destroy it. Zone-level
attributes (NS, SOA, comment, tags) and unrelated records are entirely outside this stack's reach.

If the zone ever becomes Minecraft-only (e.g., the Azure email records are retired or migrated), the data source
can be flipped to a resource block by replacing `data "aws_route53_zone"` with `resource "aws_route53_zone"`,
adding it to state via `terraform import aws_route53_zone.bytehorizonforge <zone_id>`, and updating the `aws_route53_record.mc`
reference from `data.…` to `aws_route53_zone.bytehorizonforge.zone_id`.

## Drift detection

The primary drift signal is `terraform plan` against the imported state — when something has changed in AWS that
Terraform manages, plan will show it.

For drift in resources Terraform **doesn't** manage (e.g., the LightSail key pair, instance snapshots, the
unrelated `auth.*` records in the `bytehorizonforge.com` zone), the inventory script is the cross-check:

```bash
# Keep a local baseline once you're at a known-good state.
./scripts/aws/inventory.sh
cp infra/aws-inventory.json infra/aws-inventory.baseline.json

# Later, regenerate and diff manually.
./scripts/aws/inventory.sh
diff infra/aws-inventory.baseline.json infra/aws-inventory.json
```

Both files are gitignored — the snapshot pulls in operator-specific data (cross-account bucket names, personal
domain DNS records, Azure M365 verification tokens, IAM usernames) that doesn't belong in a public repo even with
the AWS account ID redacted.

## Tags

The imported resources currently have no tags. Tags were intentionally omitted from the initial `.tf` files so the
imported state matches the live deployment exactly and `terraform plan` reports zero changes. A follow-up PR can
add `default_tags` once the import is verified — that PR's plan will show "add tags" on every resource, which is
expected.

## File layout

| File              | Concern                                                                |
|-------------------|------------------------------------------------------------------------|
| `providers.tf`    | Terraform + AWS provider version pins; S3 backend block.               |
| `variables.tf`    | `region`, `aws_profile`.                                               |
| `lightsail.tf`    | LightSail instance + firewall rules.                                   |
| `storage.tf`      | Attached data disk + disk attachment.                                  |
| `networking.tf`   | Static IP + IP-to-instance attachment.                                 |
| `dns.tf`          | `bytehorizonforge.com` Route 53 zone + `mc.` A record.                 |
| `outputs.tf`      | Public IP, DNS name, name servers.                                     |
| `backend-configs/prod.hcl.example` | Template for the operator's gitignored `prod.hcl`.    |
| `aws-inventory.json` | Local-only AWS-state snapshot (gitignored). Generated by `scripts/aws/inventory.sh`. |

## Local state hygiene

- `.terraform/` — gitignored.
- `terraform.tfstate*` — gitignored (and not relevant locally; state lives in S3).
- `terraform.auto.tfvars` — gitignored. Only `terraform.auto.tfvars.example` is committed.
- `.terraform.lock.hcl` — committed.
- `aws-inventory.json` — gitignored. Local-only output of `scripts/aws/inventory.sh`.
