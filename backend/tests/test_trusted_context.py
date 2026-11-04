"""Signed service contexts: purpose keys, canonical claims, verification and the signing secret's lifetime."""
import time
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from botocore.exceptions import ClientError
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import commit, record
from traffic_simulator.store import Store

from backend import runtime_client, trusted_context
from backend.evacuation.agent_broker import execute_tool
from backend.tests.builders import MISSING, canonical, claims_for, forged, prepared

SECRET_ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:wildfire-context-AbCdEf"
# The key-derivation test vectors: HMAC-SHA256(secret, "wildfire-signed-context-v1:" + purpose). The Runtime's
# checks (backend/runtime/tests/) assert the same keys, so both sides derive identical ones.
VECTOR_SECRET = "fixture-signing-secret-for-key-derivation-tests-only"
PURPOSE_KEYS = {"simulation": "1f583bd646287ef5450fa4ebc168be5199fe5037d017f7705f0bf271f6e8594e",
                "investigation": "90fd3123d4bc1e9034e163c7591c44afcda0726864fba97ef4bd6eebaf76a4f9",
                "firetruck_report": "5c40fcda450dc8512906822d04f44c9829b1f2e54ace93190d6d1fbd4a584307"}


def test_context_signature_expiry_and_operation_scope(tmp_path, monkeypatch):
    """A simulation context verifies only untampered, unexpired and for its own purpose, and scopes the tools."""
    monkeypatch.setenv("SIMULATION_CONTEXT_SECRET", "test-secret-long-enough-for-signed-context")
    trusted_context.clear_secret_cache()
    store = Store(tmp_path)
    session, incident, _ = prepared(store)
    turn_id = str(uuid4())
    value = record("turn", turn_id, "alice", status="running", lease_until=time.time()+180, effects=[])
    commit(store, [(value, None)])
    claims = {"sub": "alice", "session_id": session["session_id"], "incident_id": incident["incident_id"],
              "location_revision": 1, "turn_id": turn_id, "attempt_id": str(uuid4()),
              "allowed_operations": ["get_simulation_status"]}
    token = trusted_context.sign(claims, "simulation")
    assert trusted_context.verify(token, "simulation")["sub"] == "alice"
    with pytest.raises(IntegrationError) as error:
        execute_tool(store, "submit_simulation", {}, claims)
    assert error.value.status == 403
    with pytest.raises(trusted_context.jwt.PyJWTError):
        trusted_context.verify(token[:-8] + "tampered", "simulation")
    # Signed for another purpose, the same claims never authorize a tool call.
    with pytest.raises(trusted_context.jwt.InvalidSignatureError):
        trusted_context.verify(trusted_context.sign({**claims, "allowed_operations": []}, "investigation"),
                               "simulation")
    issued = int(time.time())-600
    expired = trusted_context.jwt.encode(
        {**claims, "purpose": "simulation", "iss": trusted_context.ISSUER, "aud": trusted_context.AUDIENCE,
         "iat": issued, "nbf": issued, "exp": issued+240},
        trusted_context.purpose_key(trusted_context.secret(), "simulation"), algorithm="HS256")
    with pytest.raises(trusted_context.jwt.ExpiredSignatureError):
        trusted_context.verify(expired, "simulation")
    trusted_context.clear_secret_cache()


@pytest.mark.parametrize("value", [
    "wildfire-simulation-context", "configured-server-runtime", "arn:aws:secretsmanager",
    "arn:aws:secretsmanager::123456789012:secret:context", "arn:aws:secretsmanager:us-west-2:secret:context",
])
def test_arn_region_requires_a_full_arn_and_names_the_variable(value):
    with pytest.raises(RuntimeError, match="SIMULATION_CONTEXT_SECRET_ARN must be a full ARN"):
        trusted_context.arn_region(value, "SIMULATION_CONTEXT_SECRET_ARN")


def test_secret_name_or_non_arn_runtime_fail_before_any_aws_client(monkeypatch, signing_secret):
    """A secret name or partial ARN fails naming its variable before any AWS client is created."""
    monkeypatch.setattr(trusted_context.boto3, "client", lambda *a, **k: pytest.fail("AWS client created"))
    monkeypatch.delenv("SIMULATION_CONTEXT_SECRET", raising=False)
    monkeypatch.setenv("SIMULATION_CONTEXT_SECRET_ARN", "wildfire-simulation-context")
    with pytest.raises(RuntimeError, match="SIMULATION_CONTEXT_SECRET_ARN"):
        trusted_context.secret()
    # A Runtime name or partial ARN is no binding at all, as workspace-config also reports.
    monkeypatch.setenv("SIMULATION_RUNTIME_ARN", "configured-server-runtime")
    with pytest.raises(IntegrationError) as error:
        next(runtime_client.invoke_runtime({"threadId": str(uuid4())}))
    assert error.value.code == "AGENT_NOT_CONFIGURED" and error.value.status == 503


def test_full_secret_arn_selects_its_own_region(monkeypatch, signing_secret):
    """A full secret ARN names the Region of the Secrets Manager client that reads it."""
    calls = []

    class Secrets:
        def get_secret_value(self, SecretId):
            calls.append(SecretId)
            return {"SecretString": "fixture-context-secret-long-enough-for-hmac"}

    def client(service, region_name, **kwargs):
        calls.append((service, region_name))
        return Secrets()
    monkeypatch.setattr(trusted_context.boto3, "client", client)
    monkeypatch.delenv("SIMULATION_CONTEXT_SECRET", raising=False)
    monkeypatch.setenv("SIMULATION_CONTEXT_SECRET_ARN", SECRET_ARN)
    assert trusted_context.secret() == "fixture-context-secret-long-enough-for-hmac"
    assert calls == [("secretsmanager", "us-west-2"), SECRET_ARN]


def test_each_context_purpose_has_its_own_derived_key():
    """The derived HS256 keys match the shared test vectors, and any other purpose is refused."""
    assert {purpose: trusted_context.purpose_key(VECTOR_SECRET, purpose).hex()
            for purpose in trusted_context.PURPOSES} == PURPOSE_KEYS
    for purpose in ("admin", "", "Simulation", None):
        with pytest.raises(ValueError, match="Unknown service context purpose"):
            trusted_context.purpose_key(VECTOR_SECRET, purpose)


def test_a_context_verifies_only_for_the_purpose_it_was_signed_for(monkeypatch, signing_secret):
    """Each purpose signs with its derived key, never the secret itself, and verifies for that purpose alone."""
    monkeypatch.setenv("SIMULATION_CONTEXT_SECRET", VECTOR_SECRET)
    options = {"algorithms": ["HS256"], "audience": trusted_context.AUDIENCE, "issuer": trusted_context.ISSUER}
    for signer in trusted_context.PURPOSES:
        token = trusted_context.sign(canonical(signer), signer)
        assert jwt.get_unverified_header(token) == {"alg": "HS256", "typ": "JWT"}
        claims = jwt.decode(token, bytes.fromhex(PURPOSE_KEYS[signer]), **options)
        assert claims["purpose"] == signer and claims["nbf"] == claims["iat"] and claims["exp"]-claims["iat"] == 240
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(token, VECTOR_SECRET, **options)
        # A token in the earlier format, signed with the secret itself, verifies for no purpose.
        legacy = jwt.encode(claims, VECTOR_SECRET, algorithm="HS256")
        for checker in trusted_context.PURPOSES:
            if checker == signer:
                assert trusted_context.verify(token, checker) == claims
            else:
                with pytest.raises(jwt.InvalidSignatureError):
                    trusted_context.verify(token, checker)
            with pytest.raises(jwt.InvalidSignatureError):
                trusted_context.verify(legacy, checker)


def test_the_signer_writes_the_purpose_and_refuses_another(signing_secret):
    """The signer sets the purpose claim itself and refuses a context naming another or an unknown purpose."""
    with pytest.raises(ValueError, match="purpose"):
        trusted_context.sign({**canonical("simulation"), "purpose": "investigation"}, "simulation")
    with pytest.raises(ValueError, match="Unknown service context purpose"):
        trusted_context.sign(canonical("simulation"), "admin")
    token = trusted_context.sign({**canonical("investigation"), "purpose": "investigation"}, "investigation")
    assert trusted_context.verify(token, "investigation")["purpose"] == "investigation"


@pytest.mark.parametrize("purpose, changes", [
    ("simulation", {"purpose": MISSING}), ("simulation", {"purpose": "investigation"}),
    ("simulation", {"iss": "another-broker"}), ("simulation", {"aud": [trusted_context.AUDIENCE]}),
    ("simulation", {"iat": 1.5}), ("simulation", {"iat": True}), ("simulation", {"nbf": "0"}),
    ("simulation", {"exp": [1]}), ("simulation", {"exp": {"at": 1}}), ("simulation", {"exp": float("inf")}),
    ("simulation", {"exp": lambda now: now+301}), ("simulation", {"exp": lambda now: now}),
    ("simulation", {"exp": lambda now: now-60}), ("simulation", {"nbf": lambda now: now+60}),
    ("simulation", {"iat": lambda now: now+60, "exp": lambda now: now+120}),
    ("simulation", {"sub": ""}), ("simulation", {"sub": MISSING}), ("simulation", {"session_id": 5}),
    ("simulation", {"session_id": MISSING}), ("simulation", {"allowed_operations": "get_simulation_status"}),
    ("simulation", {"allowed_operations": ["delete_everything"]}), ("simulation", {"allowed_operations": [1]}),
    ("simulation", {"incident_id": MISSING}), ("simulation", {"attempt_id": MISSING}),
    ("simulation", {"attempt_id": ""}), ("simulation", {"turn_id": "not-a-uuid"}),
    ("simulation", {"turn_id": MISSING}), ("simulation", {"location_revision": 0}),
    ("simulation", {"location_revision": "1"}), ("simulation", {"location_revision": True}),
    ("simulation", {"location_revision": MISSING}), ("simulation", {"draft_id": 5}),
    ("investigation", {"allowed_operations": ["get_simulation_status"]}),
    ("investigation", {"allowed_operations": MISSING}),
    ("firetruck_report", {"context_hash": "A"*64}), ("firetruck_report", {"context_hash": "a"*63}),
    ("firetruck_report", {"context_hash": MISSING}), ("firetruck_report", {"allowed_operations": ["x"]}),
])
def test_verifiers_refuse_any_claim_outside_the_canonical_set(signing_secret, purpose, changes):
    """A correctly signed context with one wrong, missing or mistyped claim is a rejection, never another error."""
    assert trusted_context.verify(forged(claims_for(purpose), purpose), purpose)["purpose"] == purpose
    with pytest.raises((ValueError, jwt.PyJWTError)):
        trusted_context.verify(forged(claims_for(purpose, **changes), purpose), purpose)


@pytest.mark.parametrize("token", [
    None, 12345, "", "header.payload", "a.b.c.d", "!!!.???.###",
    "W10.e30.c2ln",  # a header that is a JSON array, not an object
    jwt.encode({"sub": "alice"}, "k"*64, algorithm="HS512"),
    jwt.encode({"sub": "alice"}, None, algorithm="none"),
])
def test_malformed_contexts_are_rejections(signing_secret, token):
    """Malformed tokens and other algorithms raise the verifier's rejection errors only."""
    with pytest.raises((ValueError, jwt.PyJWTError)):
        trusted_context.verify(token, "simulation")


def test_the_signing_secret_is_cached_for_its_lifetime_and_read_again_after_it(monkeypatch, signing_secret):
    """The signing secret is read again after MAX_LIFETIME_SECONDS (300 s); a failed read never falls back."""
    # The cache lifetime is the longest lifetime a signed context may claim, a constant (decision D38).
    assert trusted_context.MAX_LIFETIME_SECONDS == 300
    clock = [1000.0]
    monkeypatch.setattr(trusted_context, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    values, reads = ["first-signing-secret-long-enough-for-hmac", "rotated-signing-secret-long-enough-for-hmac"], []

    class Secrets:
        def get_secret_value(self, SecretId):
            """Return the next stored secret value, then refuse access as a revoked read would."""
            reads.append(SecretId)
            if values:
                return {"SecretString": values.pop(0)}
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "GetSecretValue")
    monkeypatch.setattr(trusted_context.boto3, "client", lambda service, region_name, **kwargs: Secrets())
    monkeypatch.delenv("SIMULATION_CONTEXT_SECRET")
    monkeypatch.setenv("SIMULATION_CONTEXT_SECRET_ARN", SECRET_ARN)
    token = trusted_context.sign(canonical("simulation"), "simulation")
    clock[0] += 299
    assert trusted_context.verify(token, "simulation")["sub"] == "alice"
    assert reads == [SECRET_ARN]
    clock[0] += 2
    # Past the lifetime the rotated secret applies without a restart, so the earlier token no longer verifies.
    with pytest.raises(jwt.InvalidSignatureError):
        trusted_context.verify(token, "simulation")
    assert reads == [SECRET_ARN] * 2
    clock[0] += 301
    for _ in range(2):
        with pytest.raises(RuntimeError, match="SIMULATION_CONTEXT_SECRET_ARN") as error:
            trusted_context.secret()
        assert "rotated" not in str(error.value)
    assert reads == [SECRET_ARN] * 4


def test_a_missing_or_short_signing_secret_fails_naming_its_variable(monkeypatch, signing_secret):
    """A missing or short signing secret raises RuntimeError naming SIMULATION_CONTEXT_SECRET_ARN, never the value."""
    for value in ("", "too-short-for-hmac"):
        monkeypatch.setenv("SIMULATION_CONTEXT_SECRET", value)
        trusted_context.clear_secret_cache()
        with pytest.raises(RuntimeError, match="SIMULATION_CONTEXT_SECRET_ARN") as error:
            trusted_context.secret()
        assert not value or value not in str(error.value)
