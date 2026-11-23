"""Generate deploy/mosquitto/passwd from .env (hub account + one account per device).

Usage (from the repo root; needs Docker):

    uv run python deploy/mosquitto/make_passwd.py

Accounts:
* HUB_MQTT_USERNAME / HUB_MQTT_PASSWORD  (the hub itself)
* every device_id in HUB_DEVICE_TOKENS, with its token as password

Secrets are written to the file in plaintext first, then hashed in place by
``mosquitto_passwd -U`` inside the official image, so they never appear on a
command line or in the process list. The resulting file is gitignored.
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path

from hub.settings import Settings

ROOT = Path(__file__).resolve().parents[2]

IMAGE = "eclipse-mosquitto:2"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", type=Path, default=ROOT / "deploy" / "mosquitto" / "passwd")
    args = parser.parse_args()
    s = Settings()
    if not s.mqtt_password or not s.mqtt_username:
        print("HUB_MQTT_USERNAME and HUB_MQTT_PASSWORD must be set in .env", file=sys.stderr)
        return 2
    accounts = {s.mqtt_username: s.mqtt_password.get_secret_value()}
    accounts.update({dev: tok.get_secret_value() for dev, tok in s.device_tokens.items()})
    for user, pw in accounts.items():
        if ":" in user or "\n" in user or "\n" in pw:
            print(f"invalid characters in account {user!r}", file=sys.stderr)
            return 2

    passwd: Path = args.out.resolve()
    out_dir = passwd.parent
    passwd.write_text("".join(f"{u}:{p}\n" for u, p in accounts.items()), encoding="utf-8")
    os.chmod(passwd, stat.S_IRUSR | stat.S_IWUSR)
    cmd = [
        "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{out_dir}:/work", IMAGE, "mosquitto_passwd", "-U", f"/work/{passwd.name}",
    ]  # fmt: skip
    result = subprocess.run(cmd, check=False)  # noqa: S603 - fixed argv, no shell
    if result.returncode != 0:
        passwd.unlink(missing_ok=True)  # never leave plaintext behind
        print("mosquitto_passwd failed; plaintext file removed", file=sys.stderr)
        return result.returncode
    # Mosquitto runs as uid 1883 in the container and must be able to read it.
    os.chmod(passwd, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    print(f"wrote {passwd} with {len(accounts)} account(s): {', '.join(accounts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
