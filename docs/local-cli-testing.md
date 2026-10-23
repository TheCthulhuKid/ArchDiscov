# Testing the CLI locally

This page walks through the `archdiscov` commands on your own machine, offline, using the
fixtures committed under `tests/fixtures/`. Nothing here needs AWS credentials, a provider
key, or network access, except the last section on asking a real provider, which is
optional and costs tokens. For what each command does, see the
[README command reference](../README.md#cli-commands) and the linked pages.

## Set up

Install the project and its development tools into the virtual environment, then put the
installed command on your `PATH` for this shell:

```sh
uv sync --locked --group dev
source .venv/bin/activate
archdiscov --version
```

`python -m archdiscov` takes the same arguments. Run the examples from the repository root,
and write their output to a scratch directory, because no command overwrites an existing
file:

```sh
export WORK="$(mktemp -d)"
export FIXTURES=tests/fixtures
```

Every command prints a readable summary by default and JSON with `--json`, and reports
problems as diagnostics with a stable exit code. Check the exit code with `echo $?`.

## Import and inspect a snapshot

Import a snapshot, then look at one evidence record and at the recorded resources:

```sh
archdiscov ingest $FIXTURES/snapshots/network.json --output $WORK/network.json
archdiscov evidence sg-rule --snapshot $WORK/network.json
archdiscov query search --snapshot $WORK/network.json --limit 5
archdiscov query search --snapshot $WORK/network.json --type AWS::EC2::SecurityGroup --json
```

Follow one resource through its relationships:

```sh
archdiscov query inspect 123456789012/eu-west-1/ec2/physical/sg-db --snapshot $WORK/network.json
archdiscov query neighbours 123456789012/eu-west-1/ec2/physical/sg-db \
  --snapshot $WORK/network.json --direction incoming
archdiscov query paths 123456789012/eu-west-1/ec2/physical/eni-web \
  --snapshot $WORK/network.json --depth 3
```

`--json` output pipes into other tools, for example
`archdiscov query search --snapshot $WORK/network.json --json | jq '.resources[].id'`.

## Import Terraform and CloudFormation

Raw infrastructure-as-code exports need their format and the account context, because
formats are never guessed:

```sh
archdiscov ingest $FIXTURES/terraform/state.json --output $WORK/state.json \
  --format terraform-state --account-id 123456789012 --region eu-west-1
archdiscov ingest $FIXTURES/terraform/plan.json --output $WORK/plan.json \
  --format terraform-plan --account-id 123456789012 --region eu-west-1
archdiscov ingest $FIXTURES/cloudformation/template.yaml --output $WORK/template.json \
  --format cloudformation --stack-name demo --account-id 123456789012 --region eu-west-1
```

These imports report `PARTIAL` coverage: an export records only what it declares, and the
summary says which operations were incomplete.

## Run the analyzers

Each analyzer saves a verified result; `findings` shows it again after checking it against
its snapshot:

```sh
archdiscov analyze --snapshot $FIXTURES/snapshots/aws.json \
  --output $WORK/ownership.json --analyzer ownership
archdiscov findings $WORK/ownership.json --snapshot $FIXTURES/snapshots/aws.json
archdiscov analyze --snapshot $FIXTURES/snapshots/network.json \
  --output $WORK/sg-impact.json --analyzer security_group_impact \
  --resource-id 123456789012/eu-west-1/ec2/physical/sg-db
archdiscov analyze --snapshot $FIXTURES/snapshots/network.json \
  --output $WORK/exposure.json --analyzer internet_exposure --max-depth 3
archdiscov analyze --snapshot $FIXTURES/snapshots/iam.json \
  --output $WORK/trust.json --analyzer cross_account_trust
```

The committed fixtures are small, so some findings are `insufficient_evidence`. For
richer data, the test suite's audit scenarios build one snapshot per analyzer, and print
the parameters each one is meant to be run with:

```sh
.venv/bin/python -c "
from pathlib import Path
from archdiscov.services.snapshots import save_snapshot
from tests.support.audit_scenarios import SCENARIOS
for kind, scenario in SCENARIOS.items():
    save_snapshot(scenario.build(), Path('$WORK') / f'{kind.value}-scenario.json')
    print(kind.value, scenario.parameters)
"
```

For example, the database-access scenario takes its database and port:

```sh
archdiscov analyze --snapshot $WORK/database_access-scenario.json \
  --output $WORK/database-access.json --analyzer database_access \
  --resource-id 123456789012/us-east-1/rds/physical/arn%3Aaws%3Ards%3Aus-east-1%3A123456789012%3Adb%3Awriter \
  --port 5432
```

## Compare snapshots

Compare the Terraform state with the plan imported above, then show the saved comparison:

```sh
archdiscov compare --left $WORK/state.json --right $WORK/plan.json \
  --output $WORK/state-plan.json
archdiscov comparison $WORK/state-plan.json --left $WORK/state.json --right $WORK/plan.json
```

Running the same `compare` again exits with 3 (`snapshot_conflict`): results are never
overwritten, so choose a new `--output`.

## Ask a question with the fake provider

`ask` needs a provider configuration. The scripted fake provider replays a transcript of
model turns, so you can try the whole answer pipeline (tools, validation, repair, and
rendering) without a key. A transcript must use only what the tools return; the simplest
valid one searches, finds nothing to cite, and says so. Save it as
`$WORK/transcript.json`:

```json
{
  "turns": [
    {
      "kind": "respond",
      "response": {
        "message": {
          "role": "assistant",
          "tool_calls": [
            {"id": "call_1", "name": "search_resources",
             "arguments": {"kind": "parsed", "value": {"type": "AWS::RDS::DBInstance"}}}
          ]
        },
        "stop_reason": "tool_use"
      }
    },
    {
      "kind": "respond",
      "response": {
        "message": {
          "role": "assistant",
          "tool_calls": [
            {"id": "call_2", "name": "submit_answer",
             "arguments": {"kind": "parsed", "value": {
               "status": "insufficient_evidence",
               "contexts": [{"side": "primary", "source_kind": "mock"}],
               "insufficient_reason": "no_applicable_findings",
               "summary": "No database was recorded in this snapshot."
             }}}
          ]
        },
        "stop_reason": "tool_use"
      }
    }
  ]
}
```

Then point a configuration at it (relative paths resolve against the configuration file)
and ask:

```sh
printf 'kind = "fake"\ntranscript = "transcript.json"\n' > $WORK/fake.toml
archdiscov ask "Which services can access this database?" \
  --snapshot $FIXTURES/snapshots/network.json --config $WORK/fake.toml \
  --transcript $WORK/run.json
```

The answer is `insufficient_evidence` with exit code 0, and `$WORK/run.json` records the
run. Change the transcript to see the failures. Replacing the second turn with two turns
that answer in plain text (`"message": {"role": "assistant", "text": "..."}` with
`"stop_reason": "end"`) ends with `no_proposal_after_repair` (exit 5): the first plain answer
gets the one repair request, and the second fails it. A transcript that runs out of turns,
such as a single plain-text turn with no turn left for the repair, is a scripting error
(exit 2). The [fake provider](assistant-providers.md#fake-provider) page describes the
transcript format.

To see full answers to the README example questions, let the test helper write a snapshot,
a transcript built from what the tools return, and a configuration:

```sh
mkdir $WORK/owners
.venv/bin/python -c "
from pathlib import Path
from tests.support.ask_examples import EXAMPLES, write_example
write_example(EXAMPLES['ownership'], Path('$WORK/owners'))
"
archdiscov ask "Which resources appear to have no clear owner?" \
  --snapshot $WORK/owners/snapshot.json --config $WORK/owners/provider.toml
```

The other examples are `internet_exposure`, `database_access`, `security_group_impact`,
`cross_account_trust`, and four variants listed in `tests/support/ask_examples.py`; each
writes the question as the first of its arguments.

## Ask a real provider (optional)

With an Anthropic API key in your environment, a configuration that names the variable
holding it sends the question to the [Anthropic adapter](assistant-providers.md#anthropic-adapter).
This uses the network and spends tokens; the limits below keep a run small:

```sh
printf 'kind = "anthropic"\nmodel = "claude-sonnet-5-5"\napi_key_env = "ANTHROPIC_API_KEY"\nmax_requests = 6\n' \
  > $WORK/anthropic.toml
archdiscov ask "Which resources appear to have no clear owner?" \
  --snapshot $WORK/owners/snapshot.json --config $WORK/anthropic.toml \
  --transcript $WORK/anthropic-run.json
```

An OpenAI key, or a compatible server such as a gateway or a local model server, works the
same way through the
[OpenAI-compatible adapter](assistant-providers.md#openai-compatible-adapter); the
configuration also names the API root, the protocol, and what the model can do:

```sh
printf '%s\n' 'kind = "openai_compatible"' 'model = "gpt-5"' 'api_key_env = "OPENAI_API_KEY"' \
  'endpoint = "https://api.openai.com/v1"' 'protocol = "responses"' 'max_requests = 6' \
  > $WORK/openai.toml
archdiscov ask "Which resources appear to have no clear owner?" \
  --snapshot $WORK/owners/snapshot.json --config $WORK/openai.toml \
  --transcript $WORK/openai-run.json
```

A local server on `localhost` may use plain HTTP, such as
`endpoint = "http://localhost:11434/v1"` with `protocol = "chat_completions"`. `ask` always
offers tools, so a model that cannot call them cannot answer: leave `tool_calling` out of
`capabilities` for it and the run is refused before any request.

Amazon Bedrock needs no key: the [Bedrock adapter](assistant-providers.md#bedrock-adapter)
signs with its own AWS profile, separate from the one collection uses, whose identity needs
`bedrock:InvokeModel` on a model that supports tool use:

```sh
printf '%s\n' 'kind = "bedrock"' 'model = "eu.anthropic.claude-sonnet-5-5-v1:0"' \
  'profile = "assistant"' 'region = "eu-west-1"' 'max_requests = 6' > $WORK/bedrock.toml
archdiscov ask "Which resources appear to have no clear owner?" \
  --snapshot $WORK/owners/snapshot.json --config $WORK/bedrock.toml \
  --transcript $WORK/bedrock-run.json
```

The key is read only from the configured variable, never from an option or the file.
Without it the command exits with 2 (`provider_credential_missing`) before any request, and
a Bedrock profile that cannot be loaded exits with 2 (`provider_profile_unavailable`); an
API failure ends the run with 6 and the provider error it maps to
([Anthropic](assistant-providers.md#failures-and-retries),
[OpenAI-compatible](assistant-providers.md#openai-compatible-failures),
[Bedrock](assistant-providers.md#bedrock-failures)).

## Run the automated checks

The same checks CI runs, all offline:

```sh
.venv/bin/python -m pytest -n 8 --dist worksteal --ignore=tests/acceptance
.venv/bin/python -m pytest tests/acceptance -n 8 --dist loadgroup
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

For a narrower loop while changing the CLI, run its tests or one acceptance journey:

```sh
.venv/bin/python -m pytest tests/cli -n 8
.venv/bin/python -m pytest tests/acceptance/test_installed_ask.py
```

The installed-CLI acceptance tests run the command outside the checkout through both the
`archdiscov` script and `python -m archdiscov`, with a guard that fails and records any
network use. The live smoke tests against the real
[Anthropic](assistant-providers.md#live-smoke-test),
[OpenAI-compatible](assistant-providers.md#openai-compatible-live-smoke-tests), and
[Bedrock](assistant-providers.md#bedrock-live-smoke-test) APIs are deselected by default
and run only with `-m live` and their variables.
