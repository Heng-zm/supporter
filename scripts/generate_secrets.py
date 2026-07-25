"""Generate strong values for VISIT_HASH_SALT and SUPPORTERS_ADMIN_KEY."""

import secrets


if __name__ == "__main__":
    print(f"VISIT_HASH_SALT={secrets.token_urlsafe(48)}")
    print(f"SUPPORTERS_ADMIN_KEY={secrets.token_urlsafe(48)}")
