import unittest

from scripts.verify_native_tool_live import (
    admin_auth_headers,
    build_check_plan,
    build_parser,
    extract_log_items,
    find_native_tool_log,
    proxy_auth_headers,
    validate_native_tool_log,
)


class FakeConfig:
    def __init__(self, api_key="", tokens=None):
        self.api_key = api_key
        self.tokens = tokens if tokens is not None else []

    def get(self, *keys, default=None):
        if keys == ("proxy", "api_key"):
            return self.api_key
        if keys == ("tokens",):
            return self.tokens
        return default


class NativeToolLiveHelpersTest(unittest.TestCase):
    def test_build_check_plan_expands_protocol_and_mode(self):
        self.assertEqual(build_check_plan("openai", "stream"), ["openai_stream"])
        self.assertEqual(
            build_check_plan("openai", "both"),
            ["openai_stream", "openai_non_stream"],
        )
        self.assertEqual(
            build_check_plan("both", "non-stream"),
            ["openai_non_stream", "claude_non_stream"],
        )
        self.assertEqual(
            build_check_plan("both", "both"),
            [
                "openai_stream",
                "openai_non_stream",
                "claude_stream",
                "claude_non_stream",
            ],
        )

    def test_parser_accepts_protocol_and_mode_options(self):
        args = build_parser().parse_args(["--protocol", "both", "--mode", "non-stream"])

        self.assertEqual(args.protocol, "both")
        self.assertEqual(args.mode, "non-stream")

    def test_proxy_auth_headers_use_configured_api_key(self):
        headers = proxy_auth_headers(FakeConfig(api_key="sk-proxy"))

        self.assertEqual(headers, {"Authorization": "Bearer sk-proxy"})

    def test_proxy_auth_headers_require_api_key_when_token_pool_exists(self):
        with self.assertRaises(RuntimeError) as ctx:
            proxy_auth_headers(FakeConfig(api_key="", tokens=[{"value": "tabbit-token"}]))

        self.assertIn("proxy.api_key", str(ctx.exception))

    def test_admin_auth_headers_use_bearer_token(self):
        self.assertEqual(
            admin_auth_headers("admin.jwt.token"),
            {"Authorization": "Bearer admin.jwt.token"},
        )

    def test_extract_log_items_accepts_logs_and_status_payload_shapes(self):
        logs = [{"native_tools_count": 1}]

        self.assertEqual(extract_log_items({"items": logs}), logs)
        self.assertEqual(extract_log_items({"recent_logs": logs}), logs)
        self.assertEqual(extract_log_items([]), [])

    def test_find_native_tool_log_picks_matching_successful_log(self):
        logs = [
            {
                "model": "DeepSeek-V4-Pro",
                "native_tools_count": 0,
                "native_tool_names": [],
            },
            {
                "model": "Default",
                "native_tools_count": 1,
                "native_tool_names": ["parallel_web_search"],
                "native_tools_status": ["success"],
                "native_tools_result_chars": 64,
            },
        ]

        log = find_native_tool_log(logs, "parallel_web_search")

        self.assertEqual(log["model"], "Default")

    def test_validate_native_tool_log_rejects_missing_or_failed_native_tool(self):
        with self.assertRaises(AssertionError):
            validate_native_tool_log(
                {
                    "native_tools_count": 0,
                    "native_tool_names": [],
                    "native_tools_status": [],
                    "native_tools_result_chars": 0,
                },
                "parallel_web_search",
            )

        with self.assertRaises(AssertionError):
            validate_native_tool_log(
                {
                    "native_tools_count": 1,
                    "native_tool_names": ["parallel_web_search"],
                    "native_tools_status": ["error"],
                    "native_tools_result_chars": 12,
                },
                "parallel_web_search",
            )

    def test_validate_native_tool_log_returns_summary_for_successful_log(self):
        summary = validate_native_tool_log(
            {
                "native_tools_count": 1,
                "native_tool_names": ["parallel_web_search"],
                "native_tools_status": ["success"],
                "native_tools_duration_ms": 123,
                "native_tools_result_chars": 456,
            },
            "parallel_web_search",
        )

        self.assertEqual(summary["native_tool_names"], ["parallel_web_search"])
        self.assertEqual(summary["native_tools_result_chars"], 456)
