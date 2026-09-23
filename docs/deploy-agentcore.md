# Deploying Sentinel to Bedrock and AgentCore

This is Phase 5's account-level work: everything a human has to do in a browser
or with the AWS CLI. The code is finished and tested; nothing below requires
writing any.

Do it in order. Each step is verifiable on its own, so when something fails you
know which one broke.

> **Before anything else:** `docs/aws-setup.md` steps 1-6 (account, credit,
> region, IAM user, `aws configure`). Bedrock model access has approval latency
> you do not control, so request it first and read the rest while you wait.

---

## The shape of what you are deploying

```
Your laptop                     AWS
-----------                     ---
sentinel (any command)
  -> llm.run_agent()
       SENTINEL_PROVIDER=ollama    -> a model on this laptop
       SENTINEL_PROVIDER=bedrock   -> Bedrock, with your AWS credentials
       SENTINEL_PROVIDER=agentcore -> AgentCore Runtime -> Bedrock
                                        (sentinel/interfaces/agentcore_app.py)
```

Three providers, one seam. Everything else about Sentinel is identical on all
three, and a test (`tests/test_aws.py`) pins the verdict as byte-identical
across them - swapping the provider must never change what Sentinel decides.

**What the hosted runtime is not.** It takes a system prompt and a prompt and
returns text. It never sees a session, never runs `synthesize()`, and has no
code path that can produce a verdict. The verdict is computed on the machine
that has the code, from data that never left it. That is why hosting the model
does not hand the model the decision.

---

## Step 1 - Bedrock model access

Per region, per model, and off by default.

1. Console -> **Amazon Bedrock** -> region **us-east-1** -> **Model access**.
2. Request the Anthropic Claude models. Wait for **Access granted**.
3. List what your account can actually invoke:

```bash
aws bedrock list-foundation-models --region us-east-1 --query "modelSummaries[?contains(modelId,'claude')].modelId" --output table
```

Ids differ by account and region. Whatever that prints is what you configure
below - do not copy an id out of a blog post.

Confirm an actual inference call, because listing models does not prove you can
invoke them:

```bash
aws bedrock-runtime converse --region us-east-1 --model-id us.anthropic.claude-sonnet-4-5-20250929-v1:0 --messages '[{"role":"user","content":[{"text":"Reply with the single word: ready"}]}]'
```

`AccessDeniedException` means model access or the IAM policy; `ValidationException`
usually means the model id is not one of the ones listed above.

## Step 2 - Run Sentinel on Bedrock, locally

Before hosting anything, prove the provider swap works from your own machine.

```bash
export SENTINEL_PROVIDER=bedrock
export SENTINEL_BEDROCK_MODEL=us.anthropic.claude-sonnet-4-5-20250929-v1:0   # from step 1
export SENTINEL_AWS_REGION=us-east-1

python -m sentinel.doctor .
python -m sentinel.explainer.explain <a-watched-repo>
```

`doctor` should print `[ ok ] model: Bedrock <id> in us-east-1`. If it does not,
its `fix` line names the thing to correct.

You can also pin these in the `model` section of `sentinel.config.json` instead
of exporting them, which is what the shipped build does. The environment always
wins over the file.

**This step alone completes the "swap the LLM for Bedrock" deliverable.**
Everything below is the hosting on top of it.

## Step 3 - Build and push the runtime image

AgentCore Runtime runs an **arm64** container that listens on port 8080 and
answers `POST /invocations` and `GET /ping`. `deploy/Dockerfile` builds exactly
that.

```bash
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=us-east-1
export REPO=sentinel-runtime

aws ecr create-repository --repository-name $REPO --region $AWS_REGION
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com

docker build --platform linux/arm64 -f deploy/Dockerfile -t $REPO .
docker tag $REPO:latest $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$REPO:latest
docker push $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$REPO:latest
```

Test the image locally first - it is much faster than finding out after a push:

```bash
docker run --rm -p 8080:8080 \
  -e SENTINEL_BEDROCK_MODEL=$SENTINEL_BEDROCK_MODEL \
  -e AWS_REGION=$AWS_REGION \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
  $REPO

curl -s localhost:8080/ping
curl -s -X POST localhost:8080/invocations -H 'Content-Type: application/json' \
     -d '{"system":"Reply with one word.","prompt":"Say: ready"}'
```

> The same service runs without Docker for a quick check:
> `python -m sentinel.interfaces.agentcore_app --port 8080`. That path is
> verified; the container build is not, because this machine had no Docker
> daemon running when the code was written. Expect to iterate once on it.

## Step 4 - Create the runtime

The execution role AgentCore assumes needs Bedrock inference, ECR pull, and
CloudWatch logs. Create it in IAM with a trust policy for
`bedrock-agentcore.amazonaws.com`, and attach:

| Permission | Why |
| --- | --- |
| `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` | the whole point |
| `ecr:GetAuthorizationToken`, `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer` | pull the image |
| `logs:CreateLogStream`, `logs:PutLogEvents`, `logs:CreateLogGroup` | see why it failed |

Then create the runtime:

```bash
aws bedrock-agentcore-control create-agent-runtime \
  --region $AWS_REGION \
  --agent-runtime-name sentinel \
  --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$REPO:latest\"}}" \
  --network-configuration '{"networkMode":"PUBLIC"}' \
  --role-arn arn:aws:iam::$AWS_ACCOUNT_ID:role/SentinelAgentCoreRole \
  --environment-variables SENTINEL_PROVIDER=bedrock,SENTINEL_BEDROCK_MODEL=$SENTINEL_BEDROCK_MODEL
```

Keep the `agentRuntimeArn` it returns.

> **Check these two commands against the console before trusting them.**
> AgentCore is new and its control-plane API has been changing. The console's
> "create agent runtime" flow asks for exactly the same four things - image URI,
> role, network mode, environment - so if the CLI shape has moved, the console
> is the faster path and the concepts still map one to one. There is also an
> `agentcore` CLI in the `bedrock-agentcore-starter-toolkit` package that wraps
> the build-push-create sequence in one command; Sentinel does not depend on it.

## Step 5 - Point Sentinel at it

Two ways in, and they are for different people.

**By ARN, with your own AWS credentials.** This is the developer path - nothing
to mint, and it is how you should test the deployment:

```bash
export SENTINEL_PROVIDER=agentcore
export SENTINEL_AGENTCORE_ARN=arn:aws:bedrock-agentcore:us-east-1:...:runtime/sentinel-...
export SENTINEL_AWS_REGION=us-east-1

python -m sentinel.doctor .
python -m sentinel.interfaces.query <a-watched-repo> "what did it just do?"
```

**By URL, with a bearer token.** This is the shipped app's path: a user with no
AWS account at all.

```bash
export SENTINEL_PROVIDER=agentcore
export SENTINEL_AGENTCORE_ENDPOINT=https://<your-endpoint>/invocations
export SENTINEL_AGENTCORE_TOKEN=<token>          # only if the endpoint needs one
```

The client refuses to send a diff or a token over plain `http://` to anything
but localhost, so the endpoint must be `https://`.

## Step 6 - Auth, and the thing not to do

**Never ship a key inside the app.** It is extracted in minutes, and Sentinel's
own `no-hardcoded-credentials` norm would flag it - being caught by your own
supervisor is not the demo you want.

Two workable options:

1. **AgentCore's inbound authorizer.** Configure the runtime with an OAuth/JWT
   authorizer and have the app present a token. Confirm the exact configuration
   in the console once the runtime exists.
2. **A thin proxy.** A Cloudflare Worker (or any small function) holds the AWS
   credentials, calls `invoke_agent_runtime` server-side, and exposes an
   `https://` URL that `SENTINEL_AGENTCORE_ENDPOINT` points at. More moving
   parts, but it definitely works and it is where per-user rate limiting is
   easiest to put.

Whichever you choose, the client code does not change.

## Step 7 - The cost guard

The endpoint spends a fixed credit and anyone with the URL can call it. The
limits live in `sentinel/interfaces/agentcore_app.py` and are enforced at the
door, before anything reaches Bedrock:

| Cap | Default | What it stops |
| --- | --- | --- |
| `MAX_PROMPT_CHARS` | 60,000 | one enormous diff |
| `MAX_SYSTEM_CHARS` | 8,000 | a rewritten system prompt |
| `MAX_TOKENS_CEILING` | 4,096 | a caller asking for a novel |
| `RATE_LIMIT_CALLS` / `RATE_LIMIT_WINDOW_SECONDS` | 30 per 5 min per install | one install draining the credit |

Rate limiting is per install id and lives in the process, so it resets when the
runtime scales or restarts. That is deliberate for a hackathon deployment: it
is a spend guard, not a security control. Anything stronger belongs in the
proxy from step 6.

Set a **billing alarm** on the account as well. The caps bound one caller, not
the number of callers.

## Step 8 - Confirm the interfaces use it

There is nothing extra to wire. Every interface goes through `query.answer()`
and every model call goes through `run_agent()`, so once `SENTINEL_PROVIDER` is
set they all use the hosted runtime:

```bash
python -m sentinel.interfaces.query <repo> "can we ship?"     # terminal
python -m sentinel.interfaces.telegram <repo>                 # the bot
python -m sentinel.interfaces.dashboard <repo>                # the dashboard
python -m sentinel.orchestrator.session_review <repo>         # the verdict
```

Sanity-check one of them and confirm the answer still matches what the local
model gives. It must, and the test suite says so - but the demo is worth
seeing once with your own eyes.

---

## If something goes wrong

| Symptom | Cause |
| --- | --- |
| `no AWS credentials found` | `aws configure` never ran, or the shell lost the profile |
| `AccessDeniedException` | model access not granted, or the IAM policy is missing `bedrock:InvokeModel` |
| `ValidationException` | the model id is not one of the ids step 1 printed for *this* region |
| The runtime hangs on every call | `SENTINEL_PROVIDER=agentcore` inside the container - it is calling itself. The service refuses to start this way, so check the env actually reached it |
| `HTTP 429` | the per-install rate limit in step 7 |
| Everything is slow | a cold runtime plus a large diff. The deterministic verdict never waits for the model; only the narration does |

**The rollback is one variable.** `SENTINEL_PROVIDER=ollama` puts Sentinel back
on a local model with nothing else changed, and nothing leaves the machine.
Keep that in your pocket for the demo.
