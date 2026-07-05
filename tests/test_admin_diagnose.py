import unittest

from core.model_registry import MODELS_API_PATH
from routes.admin_api import (
    run_diagnose_chat_session_check,
    run_diagnose_model_registry_check,
)


class ReportRecorder:
    def __init__(self):
        self.checks = []

    def check(self, name, status, detail=""):
        self.checks.append({"name": name, "status": status, "detail": detail})

    def by_name(self, name):
        return next(c for c in self.checks if c["name"] == name)


class FakeRegistry:
    def __init__(self, models_count=26, from_snapshot=False):
        self.ready = False
        self.refresh_force = None
        self.updated_token = None
        self._models_count = models_count
        self._from_snapshot = from_snapshot

    def update_token(self, token):
        self.updated_token = token

    async def refresh(self, force=False):
        self.refresh_force = force
        self.ready = True
        return True

    def list_models(self):
        return [{"id": f"model-{i}"} for i in range(self._models_count)]

    @property
    def snapshot_info(self):
        return {
            "from_snapshot": self._from_snapshot,
            "snapshot_count": self._models_count,
            "snapshot_age": 60,
        }


class FakeClient:
    def __init__(self):
        self.called = False
        self._server_time_synced = True
        self._server_time_offset = 2.5

    async def create_chat_session(self):
        self.called = True
        return "12345678-1234-1234-1234-123456789abc"


class FakeTokenManager:
    def __init__(self):
        self.refreshed = []

    def refresh_token_from_client(self, token_id, client):
        self.refreshed.append((token_id, client))


class AdminDiagnoseTest(unittest.IsolatedAsyncioTestCase):
    async def test_model_registry_check_uses_latest_model_config_endpoint(self):
        recorder = ReportRecorder()
        registry = FakeRegistry(models_count=26)

        await run_diagnose_model_registry_check(
            recorder.check,
            registry,
            token_value="jwt|next|device",
        )

        check = recorder.by_name("上游连通(模型配置)")
        self.assertEqual(check["status"], "pass")
        self.assertIn(MODELS_API_PATH, check["detail"])
        self.assertIn("26 个模型", check["detail"])
        self.assertNotIn("/api/v0/chat/models", check["detail"])
        self.assertIs(registry.refresh_force, True)
        self.assertEqual(registry.updated_token, "jwt|next|device")

    async def test_chat_session_check_reuses_tabbit_client_session_creation(self):
        recorder = ReportRecorder()
        client = FakeClient()
        token_manager = FakeTokenManager()

        await run_diagnose_chat_session_check(
            recorder.check,
            client,
            token_id="token-1",
            token_manager=token_manager,
        )

        session_check = recorder.by_name("建会话")
        self.assertTrue(client.called)
        self.assertEqual(session_check["status"], "pass")
        self.assertIn("session_id=12345678-1234-1234-1234-123456789abc", session_check["detail"])
        self.assertNotIn("未提取到 session_id", session_check["detail"])
        self.assertEqual(token_manager.refreshed, [("token-1", client)])

        time_check = recorder.by_name("时间同步")
        self.assertEqual(time_check["status"], "pass")
