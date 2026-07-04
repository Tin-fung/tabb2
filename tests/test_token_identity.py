import base64
import json
import unittest

from routes.admin_api import (
    is_generic_token_name,
    suggest_token_name,
    token_account_label,
)


def unsigned_jwt(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{encoded}.signature"


class TokenIdentityTest(unittest.TestCase):
    def test_extracts_email_from_google_id_token(self):
        token = unsigned_jwt({"email": "alice@example.com", "name": "Alice"})

        self.assertEqual(token_account_label(token), "alice@example.com")

    def test_extracts_name_from_tabbit_token_when_email_missing(self):
        token = unsigned_jwt({"name": "Alice Work Account", "id": "user-123"})

        self.assertEqual(token_account_label(f"{token}|next-auth|device"), "Alice Work Account")

    def test_generic_google_name_is_replaced_with_account_label(self):
        token = unsigned_jwt({"email": "alice@example.com"})

        self.assertEqual(
            suggest_token_name("Google Account", token, existing_tokens=[]),
            "alice@example.com",
        )

    def test_generated_name_is_unique(self):
        token = unsigned_jwt({"email": "alice@example.com"})

        self.assertEqual(
            suggest_token_name(
                "",
                token,
                existing_tokens=[
                    {"name": "alice@example.com"},
                    {"name": "alice@example.com #2"},
                ],
            ),
            "alice@example.com #3",
        )

    def test_generated_name_avoids_existing_generic_token_with_same_account_label(self):
        token = unsigned_jwt({"email": "alice@example.com"})

        self.assertEqual(
            suggest_token_name(
                "",
                token,
                existing_tokens=[{"name": "Google Account", "value": token}],
            ),
            "alice@example.com #2",
        )

    def test_manual_specific_name_is_preserved(self):
        token = unsigned_jwt({"email": "alice@example.com"})

        self.assertEqual(
            suggest_token_name("Work quota", token, existing_tokens=[]),
            "Work quota",
        )

    def test_google_account_variants_are_generic(self):
        self.assertTrue(is_generic_token_name("Google Account"))
        self.assertTrue(is_generic_token_name("Google Account 3"))
        self.assertFalse(is_generic_token_name("alice@example.com"))
