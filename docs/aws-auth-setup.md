# AWS CLI authentication via IAM Identity Center

This walkthrough sets up AWS CLI authentication for this repo using **AWS IAM Identity Center (IdC)** with the
AWS-managed `AdministratorAccess` permission set. It is the prerequisite for issue #4 (reverse-engineering AWS
Lightsail into Terraform); issue #5 is independent of this walkthrough.

The agent stops at every checkpoint, presents what's needed, and waits for explicit confirmation before moving on.
**The configuration is console-driven** — AWS provides no API to enable IdC, and the post-bootstrap surface is
three resources of marginal value at the current scale of one operator.

## Why IdC over IAM-user access keys

Long-lived IAM access keys are the most common cause of AWS account compromise — they get committed to git,
harvested from CI logs, or lifted from laptops. IdC issues short-lived credentials backed by an SSO token cached
on disk for 8–12 hours; the underlying access keys never exist as files. AWS recommends IdC for human users, and
the modern `sso-session` block in the AWS CLI auto-refreshes those credentials. The trade-off is a one-time
~30–45 minute setup and a daily `aws sso login` step.

## Prerequisites

- **AWS CLI v2.22 or newer.** The `sso-session` block and PKCE-by-default browser flow used here are stable from
  v2.22 onward. Install or upgrade via the [AWS CLI install guide][cli-install]; verify with `aws --version`.
- **A browser** on the same device, for the IdC sign-in flow.
- **Access to your existing AWS account's root credentials** (or an IAM user with administrative permissions) for
  Checkpoint A only.

[cli-install]: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html

## Operator vs. agent split

| Checkpoint                                  | Owner              |
|---------------------------------------------|--------------------|
| A — Enable Organizations + IdC              | Operator (console) |
| B — Create user + permission set + assign   | Operator (console) |
| C — Install AWS CLI v2.22+                  | Operator           |
| D — Run `aws configure sso`                 | Operator           |
| E — Populate `.env` with `MC_AWS_PROFILE`   | Operator           |
| F — Run `scripts/aws/verify-credentials.sh` | Agent + operator   |

## Checkpoint A — Enable Organizations + IAM Identity Center

One console session. Both Organizations and IdC are enabled together by a single button in the IdC console.

1. Sign in to the [AWS Management Console][console] as the root user (or with an IAM user that has administrative
   permissions).
2. Open the [IAM Identity Center console][idc-console]. **Set the console region first** — see the irreversible
   region warning below.
3. Under **Enable IAM Identity Center**, click **Enable**, and on the next screen confirm
   **Enable IAM Identity Center with AWS Organizations**. Follow the **Organization (recommended)** tab in
   [Enable IAM Identity Center][enable-idc].

**Important — the IdC region is irreversible.** Per AWS: "AWS Organizations can have IAM Identity Center enabled
only in a single AWS Region. After enabling IAM Identity Center, if you need to change the Region that IAM
Identity Center is enabled in, you must delete the current instance and create an instance in the other Region."
Deleting and recreating destroys all permission set assignments. Pick deliberately. For a US-based operator,
`us-east-1` or `us-east-2` is typical and convenient to colocate with LightSail resources.

**Free-tier note.** Per the AWS docs: "If you use a free tier account, creating an AWS organization automatically
upgrades your account to a paid plan with pay-as-you-go pricing. Your free tier credits expire immediately." If
you are on the free tier and this matters to you, decide before clicking **Enable**.

**Stop here and confirm with the agent before proceeding to Checkpoint B.**

[console]: https://console.aws.amazon.com/

[idc-console]: https://console.aws.amazon.com/singlesignon/

[enable-idc]: https://docs.aws.amazon.com/singlesignon/latest/userguide/get-started-enable-identity-center.html

## Checkpoint B — Create your operator user, permission set, and assignment

One console session. All three sub-steps stay inside the IdC console you already have open.

1. **Add yourself as an IdC user** in the built-in identity store. Follow [Add users to your Identity Center
   directory][add-user]. Choose **Send an email to this user with password setup instructions** and use an email
   you can access right now — the invitation expires in seven days. The username and email cannot be changed
   later.
2. **Create the `AdministratorAccess` permission set** as a **predefined permission set**, not a custom one. In
   the IdC console, under **Multi-account permissions → Permission sets → Create permission set**, select
   **Predefined permission set**, then **AdministratorAccess** from the list. Follow [Create a permission
   set][create-perm-set]. `AdministratorAccess` is an [AWS-managed policy][admin-access] that grants full access
   (`Action: *`, `Resource: *`).
3. **Assign the permission set to your user** against your AWS account. Under **Multi-account permissions → AWS
   accounts**, select your account checkbox, click **Assign users or groups**, choose your user, choose the
   `AdministratorAccess` permission set, and submit. Follow [Assign user or group access to AWS accounts][assign].
4. **Accept the invitation email and complete first sign-in**, which prompts MFA enrollment. The default IdC
   authentication mode is **"Every time they sign in (always-on)"** (see [Prompt users for MFA][mfa-prompt]),
   which means you'll be prompted to register an MFA device on first login and prompted for it on every
   subsequent sign-in. Use an authenticator app or a security key. **Do not change the MFA mode to "disabled".**

**Stop here and confirm with the agent before proceeding to Checkpoint C.** Note your **AWS access portal URL**
from the IdC console **Settings → Identity source → AWS access portal URL** — you'll need it in Checkpoint D.

[add-user]: https://docs.aws.amazon.com/singlesignon/latest/userguide/addusers.html

[create-perm-set]: https://docs.aws.amazon.com/singlesignon/latest/userguide/howtocreatepermissionset.html

[admin-access]: https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AdministratorAccess.html

[assign]: https://docs.aws.amazon.com/singlesignon/latest/userguide/assignusers.html

[mfa-prompt]: https://docs.aws.amazon.com/singlesignon/latest/userguide/mfa-getting-started.html

## Checkpoint C — Install AWS CLI v2.22 or newer

Follow the [AWS CLI install guide][cli-install] for your OS (macOS GUI installer, Linux command-line installer,
or Windows MSI). Confirm the installed version meets the v2.22+ floor:

```bash
aws --version
```

The agent will assert this in Checkpoint F. **Stop here and confirm with the agent before proceeding to
Checkpoint D.**

## Checkpoint D — Run `aws configure sso`

This step writes the `[profile <name>]` and `[sso-session <name>]` blocks to `~/.aws/config`. Use the **modern
sso-session flow**, not the legacy form: only the modern form auto-refreshes credentials from the cached SSO
token. See [Configuring IAM Identity Center authentication with the AWS CLI][cli-sso].

In a terminal, run:

```bash
aws configure sso
```

You'll see these prompts in order. Suggested answers:

| Prompt                            | Enter                                                                                 |
|-----------------------------------|---------------------------------------------------------------------------------------|
| `SSO session name (Recommended):` | A short label, e.g., `mc-aws`. Don't leave blank (that triggers legacy form).         |
| `SSO start URL [None]:`           | The AWS access portal URL from Checkpoint B (e.g., `https://<id>.awsapps.com/start`). |
| `SSO region [None]:`              | The region you enabled IdC in during Checkpoint A.                                    |
| `SSO registration scopes [None]:` | `sso:account:access` (default — sufficient for `AdministratorAccess`).                |

The CLI then opens your browser for the IdC sign-in (PKCE flow, default from v2.22+). If only one account/role is
available, the CLI auto-selects it. Continue:

| Prompt                                               | Enter                                                          |
|------------------------------------------------------|----------------------------------------------------------------|
| `Default client Region [None]:`                      | The AWS region where your LightSail / other resources live.    |
| `CLI default output format (json if not specified):` | `json` (Terraform and scripts assume JSON).                    |
| `Profile name [...]:`                                | A short name, e.g., `mc-aws`. Save this — goes in `.env` next. |

The profile name you enter here is the value you'll set as `MC_AWS_PROFILE` in Checkpoint E.

**Stop here and confirm with the agent before proceeding to Checkpoint E.**

[cli-sso]: https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html

## Checkpoint E — Populate `.env`

If `.env` does not yet exist at the repo root, copy the template:

```bash
cp .env.example .env
```

Open `.env` in your editor and set `MC_AWS_PROFILE` to the profile name from Checkpoint D:

```bash
MC_AWS_PROFILE=mc-aws
```

`.env` is gitignored. Never commit it.

**Stop here and confirm with the agent before proceeding to Checkpoint F.**

## Checkpoint F — Verify

The agent runs the verify script:

```bash
./scripts/aws/verify-credentials.sh
```

This is read-only. It asserts:

- AWS CLI v2.22+ on `PATH`.
- `MC_AWS_PROFILE` is set (from env or `.env`) and isn't a placeholder.
- The profile has a region configured.
- `aws sts get-caller-identity` succeeds with an ARN matching the IdC AdministratorAccess shape:
  `arn:aws:sts::<account-id>:assumed-role/AWSReservedSSO_AdministratorAccess_<hash>/<username>`.

A clean exit prints the profile name, region, account ID, and ARN. On any failure the script prints the
copy-paste-able recovery command (typically `aws sso login --profile <name>`) and exits non-zero.

When pasting verify output into the PR description, decide whether to redact the account ID. The ARN hash and
username are non-sensitive but identify the operator.

## Cleanup / switching identities

To clear the cached SSO token (e.g., when switching IdC users or profiles):

```bash
aws sso logout --profile "$MC_AWS_PROFILE"
```

To re-authenticate after the token expires (typically 8–12 hours):

```bash
aws sso login --profile "$MC_AWS_PROFILE"
```

## Future trigger (not a deliverable)

If a second operator joins or a CI/CD federation (e.g., GitHub Actions OIDC) is added, the IdC user, permission
set, and assignment should migrate to IaC — likely Terraform with the `aws_identitystore_user`,
`aws_ssoadmin_permission_set`, and `aws_ssoadmin_account_assignment` resources. There is no commitment of when or
whether this happens; the trigger is a real second operator or a real CI use case, not anticipated future scale.
