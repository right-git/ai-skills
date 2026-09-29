"""Known-answer tests for the two signatures that fail as opaque 401/403s.

A live run can only tell you the request was rejected, never which of the
signature-base-string details was wrong, so both are pinned to published
vectors here.
"""

from datetime import datetime

from authlib.oauth1 import ClientAuth

from models import XAccount
from platforms.x import auth_header
from utils import sigv4_headers

EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_sigv4_matches_aws_get_vanilla_vector():
    """aws-sig-v4-test-suite / get-vanilla."""

    headers = sigv4_headers(
        method="GET",
        url="https://example.amazonaws.com/",
        region="us-east-1",
        service="service",
        access_key="AKIDEXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        payload_hash=EMPTY_SHA,
        headers={"x-amz-date": "20150830T123600Z"},
        now=datetime(2015, 8, 30, 12, 36, 0),
    )
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 "
        "Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def test_sigv4_s3_signs_the_payload_hash_header():
    """S3 rejects a PUT whose x-amz-content-sha256 is not signed."""

    headers = sigv4_headers(
        method="PUT",
        url="https://acct.r2.cloudflarestorage.com/bucket/social/abc.mp4",
        region="auto",
        service="s3",
        access_key="AKIDEXAMPLE",
        secret_key="secret",
        payload_hash=EMPTY_SHA,
        headers={"content-type": "video/mp4"},
        now=datetime(2026, 9, 9, 10, 0, 0),
    )
    assert headers["x-amz-content-sha256"] == EMPTY_SHA
    assert "content-type;host;x-amz-content-sha256;x-amz-date" in (
        headers["Authorization"]
    )


def test_oauth1_matches_x_documented_example():
    """The signature from X's own 'Creating a signature' walkthrough."""

    auth = ClientAuth(
        "xvz1evFS4wEEPTGEFPHBog",
        "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        token_secret="LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
        signature_method="HMAC-SHA1",
    )
    uri = "https://api.twitter.com/1/statuses/update.json?include_entities=true"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    body = (
        "status=Hello%20Ladies%20%2B%20Gentlemen%2C%20a%20signed%20OAuth%20request%21"
    )
    params = auth.get_oauth_params(
        "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg", "1318622958"
    )
    uri, headers, body = auth._render(uri, headers, body, params)
    assert auth.get_oauth_signature("POST", uri, headers, body) == (
        "tnnArxj06cWHq44gCs1OSKk/jLY="
    )


def test_x_auth_drops_oauth_body_hash():
    """X does not implement the oauth_body_hash draft; sending it means 401."""

    account = XAccount(
        platform="x",
        name="t",
        consumer_key="ck",
        consumer_secret="cs",
        access_token="at",
        access_token_secret="ats",
    )
    ours = auth_header(account, "POST", "https://api.x.com/2/media/upload/initialize")
    assert "oauth_body_hash" not in ours["Authorization"]

    # authlib on its own would add it for any non-form body, which is the trap.
    _, plain, _ = ClientAuth("ck", "cs", token="at", token_secret="ats").sign(
        "POST",
        "https://api.x.com/2/media/upload/initialize",
        {"Content-Type": "application/json"},
        b'{"total_bytes": 1}',
    )
    assert "oauth_body_hash" in plain["Authorization"]


def test_x_auth_signs_the_query_string():
    """STATUS polling puts command/media_id in the query, so it must be signed."""

    account = XAccount(
        platform="x",
        name="t",
        consumer_key="ck",
        consumer_secret="cs",
        access_token="at",
        access_token_secret="ats",
    )
    with_query = auth_header(
        account, "GET", "https://api.x.com/2/media/upload?command=STATUS&media_id=1"
    )
    without = auth_header(account, "GET", "https://api.x.com/2/media/upload")
    assert with_query["Authorization"] != without["Authorization"]
