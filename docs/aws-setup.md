# AWS Setup — do this early

`tasks.md` Phase 0 flags this as the item to start first. Access requests and
account verification can sit in a queue for hours or days, and discovering that
in week 3 would cost us the Bedrock and AgentCore phases entirely. Everything
below is account-level work that has to be done by a human in a browser.

Only one person on the team needs to do steps 1–4. Everyone needs step 6.

---

## 1. AWS Builder ID

Required by the hackathon submission form.

1. Go to https://profile.aws.amazon.com/
2. Sign up with the email you want on the submission.
3. Verify the email, set a display name.
4. **Record the Builder ID email** — it goes in the submission form.

## 2. AWS account

The Builder ID is *not* an AWS account. You need both.

1. Go to https://portal.aws.amazon.com/billing/signup
2. You will be asked for a credit/debit card. AWS runs a small temporary
   authorisation charge to verify it, then refunds it.
3. Complete phone verification.
4. Choose the **Basic (free)** support plan.
5. Account activation is usually minutes but can take a few hours — this is the
   step that justifies doing all of this on day 1.

> Do not put the root account password or any access key into this repo, into a
> chat, or into a file Sentinel reads. `.env` is gitignored; keep credentials
> there or in the AWS CLI's own config only.

## 3. Claim the $50 hackathon credit

1. Open the hackathon page on Devpost and go to the **Resources** tab.
2. Follow the AWS credit link/code there and submit the request with the AWS
   **account ID** from step 2 (12 digits, found top-right of the AWS console).
3. Credits appear under Billing → Credits. They can take a day or two.

## 4. Pick a region and enable Bedrock model access

Bedrock model access is per-region and per-model, and it is off by default.

1. Console → **Amazon Bedrock**. Set the region to **us-east-1** (widest model
   availability, and what our docs will assume — if you pick a different one,
   tell the team so we all use the same one).
2. Left sidebar → **Model access** → **Modify model access**.
3. Request access to the Anthropic Claude models plus Amazon Nova. Most are
   granted instantly; some show "In progress" for a while.
4. Wait for status **Access granted** before moving on.

## 5. Bedrock AgentCore availability

We host on AgentCore in Phase 5. While you are in the console, confirm
**Bedrock AgentCore** is present in the chosen region. If it is not offered
there, note which region does offer it — that decides where we deploy, and it
is much cheaper to learn now than in Phase 5.

## 6. Local credentials

Each teammate does this on their own laptop.

1. Install the AWS CLI: https://aws.amazon.com/cli/
2. In the AWS console: IAM → Users → create a user for yourself with
   programmatic access and the `AmazonBedrockFullAccess` policy. **Do not use
   root account keys.**
3. Configure:

```bash
aws configure
```

Enter the access key, secret key, region (`us-east-1`), and `json` as output
format.

## 7. Confirm it actually works

This is the Phase 0 checkbox that stays open until it passes. Run it and paste
the output back to the team.

```bash
aws bedrock list-foundation-models --region us-east-1 --query "modelSummaries[?contains(modelId,'claude')].modelId" --output table
```

A table of model ids means auth and region are correct. An `AccessDeniedException`
means the IAM policy in step 6 is missing; a `Could not connect` means the region
or CLI config is wrong.

Then confirm an actual inference call — listing models does not prove you can
invoke them:

```bash
aws bedrock-runtime converse --region us-east-1 --model-id us.anthropic.claude-sonnet-4-5-20250929-v1:0 --messages '[{"role":"user","content":[{"text":"Reply with the single word: ready"}]}]'
```

If that returns text, Bedrock access is confirmed and Phase 0's last box can be
ticked. If the model id is rejected, use one of the ids the previous command
printed — available ids differ by account and region.

---

## What happens after this

Nothing in Phases 1–4 needs AWS. Sentinel runs entirely on a local Ollama model
until Phase 5, which swaps the provider inside `sentinel/llm.py` — one file.
So this setup can proceed in parallel with the build; it just must not be left
until the end.

Phase 5 itself — running on Bedrock, then hosting on AgentCore — is written up
step by step in [deploy-agentcore.md](deploy-agentcore.md). The code for it is
finished; that document is the account work that remains.
