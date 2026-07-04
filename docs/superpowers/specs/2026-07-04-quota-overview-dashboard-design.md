# Quota Overview Dashboard Design

Date: 2026-07-04
Project: tabb2
Status: approved design

## 1. Context

The current Admin "额度" page uses a single account selector and tabs for
`额度概览`, `重置券`, `签到`, and `使用记录`. This makes multi-account operation
slow because only one account can be inspected at a time.

The desired page should show all enabled accounts directly on one page. Each
account should expose its own quota overview, reset coupons, and sign-in status.
Usage records should move into a separate combined section.

## 2. Product Direction

The page becomes a multi-account quota dashboard:

- No account dropdown as the primary navigation.
- No tabbed account workflow.
- All enabled accounts are shown as repeated account panels.
- Each account panel contains a compact but complete view of:
  - account identity and status
  - quota overview
  - reset coupons
  - sign-in state and per-account sign-in action
- Usage records are consolidated into a separate page section below the account
  panels.

The existing dark Admin UI style should be preserved. This is an operational
dashboard, not a marketing page.

## 3. Goals

- Make all account quota states scannable without switching accounts.
- Keep per-account operations local to the account card.
- Separate usage records from account summary content.
- Avoid front-end request sprawl by adding a backend aggregation endpoint.
- Preserve existing coupon claim/use and sign-in capabilities.
- Keep partial failures isolated to the affected account.

## 4. Non-Goals

- Do not redesign the full Admin UI.
- Do not introduce a new visual design system.
- Do not change Tabbit quota, coupon, sign-in, or usage-record upstream API
  behavior.
- Do not remove existing individual admin endpoints unless tests prove they are
  unused.
- Do not add charts or heavy data visualization in the first implementation.

## 5. Backend API

Add a new Admin endpoint:

`GET /api/admin/quota/overview`

Response shape:

```json
{
  "ok": true,
  "accounts": [
    {
      "token_id": "token-1",
      "token_name": "alice@example.com",
      "enabled": true,
      "quota": {"ok": true, "data": {}},
      "coupons": {"ok": true, "data": {}},
      "sign_in": {"ok": true, "data": {}}
    }
  ],
  "usage_records": [
    {
      "token_id": "token-1",
      "token_name": "alice@example.com",
      "records": [],
      "ok": true
    }
  ]
}
```

Rules:

- Include all enabled tokens.
- Each account should be queried independently.
- If one account fails, return that account with `ok: false` for the failing
  subsection and continue processing other accounts.
- `usage_records` should aggregate recent records by account. The first
  implementation can keep records grouped by token instead of globally sorting
  them, as long as each row displays the account name.
- Do not expose raw token values.

Existing endpoints remain available:

- `GET /api/admin/quota`
- `GET /api/admin/coupons`
- `POST /api/admin/coupons/claim`
- `POST /api/admin/coupons/use`
- `GET /api/admin/sign-in/status`
- `POST /api/admin/sign-in`
- `GET /api/admin/usage-records`

## 6. Frontend Layout

The "额度" page should render:

1. Header band
   - Title: `额度管理`
   - Subtitle: concise operational copy
   - Primary action: `刷新全部`

2. Account panel grid
   - Responsive grid: one column on mobile, two columns on wider screens.
   - Each panel uses the existing dark card style.
   - Panel header:
     - account display name
     - token status or error badge
   - Quota block:
     - member level
     - usage percentage progress bar
     - reset time, cycle, coupon count when available
   - Reset coupon block:
     - count
     - coupon names/codes
     - per-account claim/use buttons
   - Sign-in block:
     - daily sign-in scenes
     - per-account sign-in button when not fully signed

3. Combined usage records section
   - Full-width section below account panels.
   - Table columns:
     - account
     - time
     - feature
     - usage
   - Display recent records from all accounts.
   - Show per-account failure messages in a compact list if records failed for
     any account.

## 7. Interaction Rules

- `刷新全部` calls `/api/admin/quota/overview` and re-renders the whole page.
- Per-account sign-in calls `/api/admin/sign-in?token_id=<id>`, then refreshes
  the overview.
- Per-account claim coupon calls `/api/admin/coupons/claim?token_id=<id>`, then
  refreshes the overview.
- Per-account use coupon calls
  `/api/admin/coupons/use?token_id=<id>&coupon_code=<code>`, then refreshes the
  overview.
- Empty states should be quiet and short, for example `暂无可用重置券` or
  `暂无使用记录`.
- Loading state should not shift layout dramatically.

## 8. Error Handling

- If the overview endpoint itself fails, show one page-level error panel.
- If an individual account quota/coupons/sign-in query fails, show the error
  inside that account panel.
- If usage records fail for one account, keep the rest of the combined records
  visible and show a small warning for the failed account.
- All server-provided text must be escaped before rendering.

## 9. Testing Strategy

Backend tests:

- Overview endpoint returns one account entry per enabled token.
- Overview endpoint does not include disabled tokens.
- One failing account subsection does not fail the whole response.
- Usage records include token identity metadata.

Frontend static tests:

- Quota page no longer renders the account selector or quota tabs.
- Quota page contains account panel grid markers.
- Quota page contains combined usage records markers.
- Per-account action functions pass `token_id`.
- Rendering helpers escape account names, coupon fields, and usage-record text.

Verification:

- Run focused admin quota tests.
- Run full unit test discovery.
- Run `compileall`.
- Run `pip check`.
- Run `git diff --check`.

## 10. Rollout

1. Add backend overview tests.
2. Implement `/api/admin/quota/overview`.
3. Add frontend static regression tests.
4. Replace the old quota selector/tab UI with the new dashboard layout.
5. Reuse existing rendering logic where it keeps the code simple.
6. Run full verification.
