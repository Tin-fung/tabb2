# Quota Overview Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Admin quota page's account selector and tabs with an all-account overview dashboard backed by a quota aggregation endpoint.

**Architecture:** Add a backend `build_quota_overview()` helper and `/api/admin/quota/overview` endpoint that returns per-account quota, coupons, sign-in, and grouped usage-record data. Replace the frontend quota tab workflow with a single refreshable dashboard containing account cards and a combined usage-record section.

**Tech Stack:** Python 3.11+/FastAPI, standard `unittest`, existing vanilla JavaScript/Tailwind Admin UI in `static/index.html`.

## Global Constraints

- No account dropdown as the primary navigation.
- No tabbed account workflow.
- All enabled accounts are shown as repeated account panels.
- Each account panel shows quota overview, reset coupons, and sign-in state.
- Usage records are consolidated into a separate section below the account panels.
- Preserve the existing dark Admin UI style.
- Keep partial failures isolated to the affected account.
- All server-provided text must be escaped before rendering.

---

## File Structure

- Modify `routes/admin_api.py`: add `build_quota_overview()` and `GET /api/admin/quota/overview`.
- Create `tests/test_admin_quota_overview.py`: unit tests for the aggregation helper.
- Modify `static/index.html`: replace quota selector/tabs with the dashboard layout and per-account action functions.
- Create `tests/test_admin_quota_dashboard_ui.py`: static regression tests for dashboard markup and escaping/action helpers.

---

### Task 1: Backend Quota Overview Aggregation

**Files:**
- Modify: `routes/admin_api.py`
- Create: `tests/test_admin_quota_overview.py`

**Interfaces:**
- Produces: `async def build_quota_overview(tokens: list[dict], client_for_token, usage_limit: int = 20) -> dict`
- Produces endpoint: `GET /api/admin/quota/overview`

- [ ] **Step 1: Write failing backend tests**

Create `tests/test_admin_quota_overview.py`:

```python
import unittest

from routes.admin_api import build_quota_overview


class FakeClient:
    def __init__(self, name, fail=None):
        self.name = name
        self.fail = set(fail or [])

    async def get_quota_usage(self):
        if "quota" in self.fail:
            raise RuntimeError("quota failed")
        return {"usage_percentage": "12.5%", "member_level": "pro"}

    async def get_coupon_list(self, coupon_type="weekly_reset_coupon", status=1):
        if "coupons" in self.fail:
            raise RuntimeError("coupons failed")
        return {"coupons": [{"coupon_code": f"{self.name}-coupon"}]}

    async def get_sign_in_status(self):
        if "sign_in" in self.fail:
            raise RuntimeError("sign-in failed")
        return {"results": [{"scene_code": "daily_sign_in", "signed_today": False}]}

    async def get_usage_records(self, page=1, limit=50):
        if "records" in self.fail:
            raise RuntimeError("records failed")
        return {"records": [{"request_time": "2026-07-04", "consume_percentage": "-1%"}]}


class AdminQuotaOverviewTest(unittest.IsolatedAsyncioTestCase):
    async def test_overview_includes_enabled_tokens_only_and_omits_values(self):
        tokens = [
            {"id": "t1", "name": "Account 1", "value": "secret-1", "enabled": True},
            {"id": "t2", "name": "Disabled", "value": "secret-2", "enabled": False},
        ]

        async def client_for_token(token_id):
            return tokens[0], FakeClient(token_id)

        result = await build_quota_overview(tokens, client_for_token)

        self.assertTrue(result["ok"])
        self.assertEqual([a["token_id"] for a in result["accounts"]], ["t1"])
        self.assertNotIn("value", result["accounts"][0])
        self.assertEqual(result["accounts"][0]["quota"]["data"]["member_level"], "pro")
        self.assertEqual(result["usage_records"][0]["token_name"], "Account 1")

    async def test_subsection_failure_does_not_fail_whole_overview(self):
        tokens = [{"id": "t1", "name": "Account 1", "enabled": True}]

        async def client_for_token(token_id):
            return tokens[0], FakeClient(token_id, fail={"coupons", "records"})

        result = await build_quota_overview(tokens, client_for_token)
        account = result["accounts"][0]

        self.assertTrue(result["ok"])
        self.assertTrue(account["quota"]["ok"])
        self.assertFalse(account["coupons"]["ok"])
        self.assertIn("coupons failed", account["coupons"]["error"])
        self.assertFalse(result["usage_records"][0]["ok"])
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/Users/tin/project/tabbit/tabb2/.venv/bin/python -m unittest tests.test_admin_quota_overview
```

Expected: FAIL with import error for `build_quota_overview`.

- [ ] **Step 3: Implement backend helper and endpoint**

Add `build_quota_overview()` near the admin helper functions in `routes/admin_api.py`. It should filter enabled tokens, call `client_for_token(token_id)`, catch failures per subsection, and return `{"ok": True, "accounts": [...], "usage_records": [...]}`.

Inside `init()`, add:

```python
    @r.get("/quota/overview", dependencies=[Depends(admin_dep)])
    async def query_quota_overview():
        tokens = _cfg.get("tokens", default=[]) or []

        async def client_for_token(token_id):
            return _tm.get_client_for_token(token_id)

        return await build_quota_overview(tokens, client_for_token)
```

- [ ] **Step 4: Run backend tests**

Run:

```bash
/Users/tin/project/tabbit/tabb2/.venv/bin/python -m unittest tests.test_admin_quota_overview
```

Expected: PASS.

- [ ] **Step 5: Commit backend task**

Run:

```bash
git add routes/admin_api.py tests/test_admin_quota_overview.py
git commit -m "feat(admin): aggregate quota overview data"
```

---

### Task 2: Frontend Quota Dashboard Layout

**Files:**
- Modify: `static/index.html`
- Create: `tests/test_admin_quota_dashboard_ui.py`

**Interfaces:**
- Consumes: `GET /api/admin/quota/overview`
- Produces JS functions: `runQuotaOverview()`, `renderQuotaOverview(r)`, `renderQuotaAccountCard(account)`, `renderCombinedUsageRecords(groups)`, `signInToken(tokenId)`, `claimCouponForToken(tokenId)`, `useCouponForToken(tokenId, couponCode)`

- [ ] **Step 1: Write failing frontend static tests**

Create `tests/test_admin_quota_dashboard_ui.py`:

```python
from pathlib import Path
import unittest


STATIC_INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


class AdminQuotaDashboardUiTest(unittest.TestCase):
    def test_quota_page_uses_overview_dashboard_not_selector_tabs(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("api('/quota/overview')", html)
        self.assertIn('id="quotaAccountGrid"', html)
        self.assertIn('id="usageRecordsSection"', html)
        self.assertNotIn('id="tokenSelector"', html)
        self.assertNotIn("class=\"quota-tab", html)

    def test_quota_dashboard_has_per_account_actions(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("function signInToken(tokenId)", html)
        self.assertIn("function claimCouponForToken(tokenId)", html)
        self.assertIn("function useCouponForToken(tokenId, couponCode)", html)
        self.assertIn("token_id=${encodeURIComponent(tokenId)}", html)

    def test_quota_dashboard_escapes_untrusted_fields(self):
        html = STATIC_INDEX.read_text(encoding="utf-8")

        self.assertIn("${esc(account.token_name || account.token_id)}", html)
        self.assertIn("${esc(c.coupon_code || '')}", html)
        self.assertIn("${esc(rec.scene_group_name_display || rec.scene_group_name || '-')}", html)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/Users/tin/project/tabbit/tabb2/.venv/bin/python -m unittest tests.test_admin_quota_dashboard_ui
```

Expected: FAIL because the current page still has `tokenSelector`, tabs, and no overview endpoint usage.

- [ ] **Step 3: Replace quota UI**

In `static/index.html`, replace the old quota globals and functions from `let lastQuotaResult...` through `renderRecordsResult()` with the new dashboard flow:

- `lastQuotaOverview = null`
- `renderQuota(el)` renders header, `quotaAccountGrid`, and `usageRecordsSection`
- `runQuotaOverview()` fetches `/quota/overview`
- `renderQuotaOverview(r)` fills account grid and records section
- `renderQuotaAccountCard(account)` renders quota/coupons/sign-in blocks
- per-account actions call existing endpoints with encoded `token_id`

Keep and reuse `renderQuotaCards(q)` below the replaced block.

- [ ] **Step 4: Run frontend static tests**

Run:

```bash
/Users/tin/project/tabbit/tabb2/.venv/bin/python -m unittest tests.test_admin_quota_dashboard_ui tests.test_admin_dashboard_xss
```

Expected: PASS.

- [ ] **Step 5: Commit frontend task**

Run:

```bash
git add static/index.html tests/test_admin_quota_dashboard_ui.py
git commit -m "feat(admin): show quota accounts as dashboard"
```

---

### Task 3: Full Verification

**Files:**
- Verify all changed files.

- [ ] **Step 1: Run focused tests**

Run:

```bash
/Users/tin/project/tabbit/tabb2/.venv/bin/python -m unittest tests.test_admin_quota_overview tests.test_admin_quota_dashboard_ui tests.test_admin_dashboard_xss
```

Expected: PASS.

- [ ] **Step 2: Run full verification**

Run:

```bash
/Users/tin/project/tabbit/tabb2/.venv/bin/python -m unittest discover -s tests
python3 -m compileall -q tabbit2api.py core routes scripts tests
/Users/tin/project/tabbit/tabb2/.venv/bin/python -m pip check
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 3: Commit verification/doc plan if needed**

If this plan file is still uncommitted, run:

```bash
git add docs/superpowers/plans/2026-07-04-quota-overview-dashboard.md
git commit -m "docs: plan quota overview dashboard"
```

Expected: working tree clean after final commits.
