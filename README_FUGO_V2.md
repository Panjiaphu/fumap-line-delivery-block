# FUGO Rebuild V2 — Mobile/Desktop + zh/en

Repo target:

```text
Panjiaphu/fumap-line-delivery-block
```

## Main design

LINE is the center. Each role enters its own portal.

- `/customer` — customer portal
- `/store` — store portal
- `/driver` — driver portal
- `/admin` — admin portal
- `/block` — block explorer
- `/support` — CSKH / disputes
- `/internal/proof/photo` — photo proof callback from `fumapgo-linehook`

## Mobile / Desktop separation

Backend routes are shared, templates are separated:

```text
templates/mobile/...
templates/desktop/...
static/mobile/app.css
static/desktop/app.css
```

View control:

```text
?view=mobile
?view=desktop
```

Auto-detection: LINE / iPhone / Android defaults to mobile.

## Language separation

```text
?lang=zh
?lang=en
```

Translation dictionary:

```text
services/i18n.py
```

## Render

Build:

```bash
pip install -r requirements.txt
```

Start:

```bash
gunicorn app:app
```

Required ENV:

```env
APP_NAME=FUGO｜FUMAP GO
SECRET_KEY=FUGO_SECRET_2026_CHANGE_ME
ADMIN_TOKEN=FUGO_ADMIN_2026_CHANGE_ME
DATABASE_PATH=data/fugo.db
PUBLIC_BASE_URL=https://fumapgo.onrender.com
LINEHOOK_BASE_URL=https://panjiaphu-fumapgo-linehook.onrender.com
FGO_INTERNAL_SECRET=FGO_INTERNAL_2026_PAN8893_BLOCK_SECRET
FGO_ADMIN_LINE_USER_ID=U30bb96ea9feec6a29853100df454c741
DEV_SHOW_CODES=1
DEFAULT_LANG=zh
```

## LINE rich menu URLs

```text
Customer: https://fumapgo.onrender.com/customer?view=mobile&lang=zh
Store:    https://fumapgo.onrender.com/store?view=mobile&lang=zh
Driver:   https://fumapgo.onrender.com/driver?view=mobile&lang=zh
Block:    https://fumapgo.onrender.com/block?view=mobile&lang=zh
CSKH:     https://fumapgo.onrender.com/support/new?view=mobile&lang=zh
Admin:    https://fumapgo.onrender.com/admin?view=desktop&lang=zh
```

## Smoke test after deploy

```text
/health
/debug/routes
/customer?view=mobile&lang=zh
/customer?view=desktop&lang=en
/store?view=mobile&lang=zh
/driver?view=mobile&lang=zh
/admin/login?view=desktop&lang=zh
/block?view=mobile&lang=zh
/support/new?view=mobile&lang=zh
```

## Important

This version intentionally uses a new DB path:

```env
DATABASE_PATH=data/fugo.db
```

For commercial deployment, migrate to PostgreSQL/Supabase.
