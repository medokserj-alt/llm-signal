import os

# Safe fallback allowlist for DAY/MID admin access.
ADMIN_USER_IDS: set[int] = {6308066297, 672885732}


def _parse_int_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    if not raw:
        return ids
    for part in raw.replace(";", ",").split(","):
        value = part.strip()
        if value.isdigit():
            ids.add(int(value))
    return ids


def parse_admin_ids(env_value: str | None = None) -> set[int]:
    raw = os.getenv("TG_ADMIN_IDS", "") if env_value is None else (env_value or "")
    return _parse_int_ids(raw)


def is_admin(user_id: int, env_value: str | None = None) -> bool:
    env_ids = parse_admin_ids(env_value=env_value)
    if env_ids:
        return user_id in env_ids
    return user_id in ADMIN_USER_IDS


def analysis_menu_layout(is_admin_user: bool) -> list[list[str]]:
    rows: list[list[str]] = [["📈 Current"]]
    if is_admin_user:
        rows.append(["🗓 DAY", "📰 MID"])
    rows.append(["⬅️ Назад"])
    return rows
