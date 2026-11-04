# 45. Credential rotation and revocation runbook

Every credential the deployed system uses, where it is stored, who reads it, and how to replace or revoke it. Nothing rotates automatically: third-party provider keys can only be issued in the provider's portal, and the internal keys need every reader restarted together, so this manual procedure is the rotation control. Rotate on the schedule your organization requires, and at once when a person with access leaves or a value may have leaked.

Never put a credential value on a command line, in a log, a ticket, a chat message or a file in this checkout. The procedures below pass values through a private temporary file or the Secrets Manager console.

## Credentials and their readers

Stack names follow `<name>-<deployment id>-<component>`; `bash deployment/deploy-all.sh` prints them in its plan.

| Credential | Stored in | Read by | How a new value takes effect |
|---|---|---|---|
| TomTom API key | `TOMTOM_API_KEY` in the installation's provider-input secret (`[providers] secret_arn`); the deployer copies it into the application stack's provider secret (output `ProviderSecretArn`) | API and SUMO worker ECS tasks | `deploy-all.sh --apply` copies it and restarts both services |
| NASA FIRMS map key (optional) | `NASA_FIRMS_API_KEY` in the provider-input secret; copied as `FIRMS_MAP_KEY` into the application provider secret and passed to the backend stack's API keys secret (output `APIKeysSecretArn`), whose copy has had no reader since W2.11 | API and SUMO worker tasks | `deploy-all.sh --apply` |
| Delphire camera key (optional) | `DELPHIRE_API_KEY` in the provider-input secret, copied into the application provider secret, which the API task receives whole as `PROVIDER_CREDENTIALS` | API task | `deploy-all.sh --apply` copies it and restarts the API and worker; keep the old key valid until the deploy ends |
| DTN client ID and client secret | `DTN_CLIENT_ID` and `DTN_CLIENT_SECRET` in the provider-input secret; passed to the backend API keys secret | Read by the DTN weather Gateway Lambda from the backend API keys secret by ARN | `deploy-all.sh --apply` updates the function, and its new execution environments read the current value |
| Simulation context signing key | Application stack secret `SimulationContextSecret` (output `SimulationContextSecretArn`), 64 random characters generated at creation | The workspace API signs with it and verifies the simulation tool calls; the AgentCore Runtime verifies. Each derives one key per context purpose (simulation, investigation, firetruck report), so a context signed for one purpose never verifies as another. The simulation Gateway Lambda forwards contexts without reading the key | Put a new value; the API and the Runtime both read it by ARN and pick it up within 5 minutes, without a restart |
| Firetruck service key | Application stack secret `FiretruckServiceKey` | API task (`FIRETRUCK_SERVICE_KEY`) and firetruck task | Put a new value, then `deploy-all.sh --apply` |
| Radio service key | Application stack secret `RadioServiceKey` | API task (`BROADCASTIFY_PRIVATE_KEY`) and radio task (`API_KEY`) | Put a new value, then `deploy-all.sh --apply` |
| Legacy radio connection | Application stack secret `RadioBrokerCredential` (output `RadioBrokerSecretArn`; fields `WS_URL` and `API_KEY`, blank by default) | Nothing: no task receives it, and no code has read it since the API's older radio path was removed on 2026-10-19. The secret stays, because the deploy guard stops a release that would delete a secret | Edit the fields; no reader picks them up |
| Gateway client secret | Secret of the Cognito app client in the backend stack (outputs `CognitoUserPoolId` and `CognitoClientId`). The Runtime's copy is the backend Secrets Manager secret `GatewayClientSecret` (output `GatewayClientSecretArn`); the Runtime receives only its ARN | AgentCore Runtime, which reads the copy when its Gateway client starts, for its client-credentials token request to the Gateway | Two-secret procedure below |

These need no rotation: demo session cookies are random tokens stored only as SHA-256 digests, with no signing key; Amazon Location, Bedrock and AgentCore use IAM roles; NWS, OpenStreetMap/Overpass and the public ArcGIS and Caltrans feeds take no key. Local development keeps a TomTom key in the git-ignored `traffic-simulator/.runtime/tomtom-api-key`; replace it with `traffic-simulator/scripts/configure_tomtom.py`.

Every `--apply` that is not resuming an interrupted release builds a new release: new image tags for all four ECS services, new Lambda packages and a new Runtime image. So one `--apply` restarts every reader in the table, and each reads the current secret values when it starts.

## Before you start

- Use the installation's profile and Region, for example `export AWS_PROFILE=<profile> AWS_REGION=<region>`.
- List the application stack's secrets and their ARNs (no values are returned):

  ```bash
  aws cloudformation list-stack-resources --stack-name "<name>-<deployment id>-application" \
    --query "StackResourceSummaries[?ResourceType=='AWS::SecretsManager::Secret'].[LogicalResourceId,PhysicalResourceId]" \
    --output table
  ```

- Generate random values into a private temporary file (`mktemp` creates it readable only by you), pass it with `file://`, and delete it afterwards:

  ```bash
  VALUE_FILE="$(mktemp)"
  python3 -c "import secrets; print(secrets.token_hex(32), end='')" > "$VALUE_FILE"
  # ... use "file://$VALUE_FILE" in the command that needs the value ...
  rm -f "$VALUE_FILE"
  ```

## Rotate a provider key (TomTom, FIRMS or DTN)

1. Issue a new key or client secret in the provider's portal. If the provider allows two active keys, keep the old one until step 4.
2. Edit the one field in the installation's provider-input secret (the ARN in `[providers] secret_arn`) with the Secrets Manager console or your secure provisioning workflow, as [workspace operations](29-workspace-operations.md) describes for creating it.
3. Run `bash deployment/deploy-all.sh` to check and plan, then `bash deployment/deploy-all.sh --apply`.
4. Check the result (see [Verify](#verify)), then delete or deactivate the old key in the provider's portal.

## Rotate an internal key

This covers the simulation context signing key and the firetruck and radio service keys. The application stack generated them, and nothing outside AWS uses them.

1. Find the secret's ARN (see [Before you start](#before-you-start)).
2. Write a new value and store it:

   ```bash
   VALUE_FILE="$(mktemp)"
   python3 -c "import secrets; print(secrets.token_hex(32), end='')" > "$VALUE_FILE"
   aws secretsmanager put-secret-value --secret-id "<secret ARN>" --secret-string "file://$VALUE_FILE"
   rm -f "$VALUE_FILE"
   ```

3. Run `bash deployment/deploy-all.sh --apply` so every reader restarts with the new value. The firetruck and radio service keys need it, because their tasks receive them only when they start; the signing key does not (see step 4).
4. Expect a short disruption while old and new tasks overlap, so rotate in a quiet period. A context signed with the old key fails at a reader that has the new one; signed contexts expire within five minutes (`backend/trusted_context.py`), so retry the agent turn after the deploy finishes. Radio or firetruck calls can fail until both the API and that service finish deploying.
   - Both readers of the signing key, the workspace API and the AgentCore Runtime, read it from Secrets Manager by its ARN and keep it for at most 5 minutes, the longest lifetime a signed context may claim (`MAX_LIFETIME_SECONDS` in `trusted_context.py` and `backend/runtime/utils/simulation_context.py`), then read it again.
   - So after step 2, both readers pick up a rotated signing key within 5 minutes, without a restart, even before step 3 restarts them.
   - To keep the mismatch window short, run step 3 right after step 2.

The legacy radio connection has no reader; if it holds old radio provider values, blank its `WS_URL` and `API_KEY` fields in the Secrets Manager console.

## Rotate the gateway client secret

Amazon Cognito keeps up to two active secrets for one app client, so add the new secret before removing the old one and the Runtime keeps working. This needs a current AWS CLI v2.

1. Read `CognitoUserPoolId`, `CognitoClientId` and `GatewayClientSecretArn` from the backend stack's outputs, and set `POOL`, `CLIENT` and `SECRET` to them.
2. Note the current secret's ID. The response lists IDs and creation dates, never values:

   ```bash
   aws cognito-idp list-user-pool-client-secrets --user-pool-id "$POOL" --client-id "$CLIENT"
   ```

3. Add a new secret. Cognito accepts 24 to 64 letters, digits, `_` or `+`; the generator below writes 64 hexadecimal characters:

   ```bash
   VALUE_FILE="$(mktemp)"
   python3 -c "import secrets; print(secrets.token_hex(32), end='')" > "$VALUE_FILE"
   aws cognito-idp add-user-pool-client-secret --user-pool-id "$POOL" --client-id "$CLIENT" \
     --client-secret "file://$VALUE_FILE" --query ClientSecretDescriptor.ClientSecretId --output text
   ```

4. Store the same value as the Runtime's copy, delete the file, and deploy so the Runtime starts with the new value:

   ```bash
   aws secretsmanager put-secret-value --secret-id "$SECRET" --secret-string "file://$VALUE_FILE"
   rm -f "$VALUE_FILE"
   bash deployment/deploy-all.sh --apply
   ```

   Later deployments keep this value. The backend stack copies the client secret into `GatewayClientSecret` only when its copy resource (`GatewayClientSecretCopy` in `backend/deployment/templates/nested/3-cognito-auth.yaml`) is created or its properties change, never on an ordinary `--apply`, so it does not write the old secret back while both are active. A release that would run the copy stops for review instead (see [workspace operations](29-workspace-operations.md)). Don't acknowledge that stop while both secrets are active: the copy stores whichever secret Cognito returns, which this runbook has not established. Finish step 5 first. Writing this secret by hand shows up in `check_gateway_secret verify` as a write made outside CloudFormation, which is expected here.
5. Check that an Investigation turn can call a Gateway tool, then delete the old secret by the ID from step 2:

   ```bash
   aws cognito-idp delete-user-pool-client-secret --user-pool-id "$POOL" --client-id "$CLIENT" \
     --client-secret-id "<old secret ID>"
   ```

## Revoke after a suspected leak

1. **Stop the leaked value first.**
   - Provider key: disable it in the provider's portal now. That feature stops working until step 2 finishes.
   - Internal key: put a new value and run `--apply`.
   - Gateway client secret: add a new secret, store it in `GatewayClientSecret`, run `--apply`, then delete the leaked secret right away.
2. Rotate as described above.
3. Review who read the secret: in CloudTrail event history, filter on the event name `GetSecretValue` and check the callers for that secret. Only the readers in the table should appear.
4. Record the credential's name, the date and the reason in the incident or release record, never the value.

## Verify

- `bash deployment/deploy-all.sh --apply` finishes with its smoke check passing.
- TomTom: a Traffic Lab run in traffic-speed mode completes. FIRMS: the Traffic Lab's source check reports NASA FIRMS as `available`, not `unavailable` or `not_configured`.
- DTN and the gateway client secret: an Investigation turn that asks for current weather returns DTN conditions.
- Signing key: an Investigation turn and an agent-run simulation complete.
- Service keys: the Response Tracker report and Radio Investigation work.
