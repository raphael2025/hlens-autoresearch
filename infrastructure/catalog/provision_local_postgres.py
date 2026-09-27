"""One-off local provisioning of a dedicated Iceberg catalog database + login role (C2, H12).

Operator tool, not imported by runtime code. For one profile it:

1. refuses to run if the env file, the role or the database already exists (it never alters,
   drops or reuses existing resources);
2. generates a random password and writes ``<VAR>=postgresql+psycopg2://…`` to a Git-ignored
   ``.env.*`` file created with mode 600 (``O_EXCL``);
3. sends only a client-side SCRAM-SHA-256 verifier (never the password) to
   ``sudo -n -u postgres psql`` on stdin: a ``LOGIN`` role without superuser / createdb /
   createrole / replication / bypassrls, a database owned by it, and ``REVOKE ALL … FROM PUBLIC``
   so other non-superuser roles (including the other profile's role) cannot connect.

Nothing is printed except resource names. Usage::

    uv run python -m infrastructure.catalog.provision_local_postgres catalog
    uv run python -m infrastructure.catalog.provision_local_postgres test
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = ["PROFILES", "Profile", "scram_sha256_verifier"]

_REPO_ROOT: Final = Path(__file__).resolve().parents[2]
_SCRAM_ITERATIONS: Final = 4096
_PSQL: Final = ("sudo", "-n", "-u", "postgres", "psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1")


@dataclass(frozen=True)
class Profile:
    role: str
    database: str
    env_file: str
    env_var: str


PROFILES: Final = {
    "catalog": Profile(
        "hlens_iceberg_catalog", "hlens_iceberg_catalog", ".env.catalog", "HLENS_CATALOG_URI"
    ),
    "test": Profile(
        "hlens_iceberg_catalog_test",
        "hlens_iceberg_catalog_test",
        ".env.catalog-test",
        "HLENS_TEST_CATALOG_URI",
    ),
}


def scram_sha256_verifier(password: str, salt: bytes, iterations: int = _SCRAM_ITERATIONS) -> str:
    """PostgreSQL ``SCRAM-SHA-256$<iter>:<salt>$<StoredKey>:<ServerKey>`` (RFC 5802 / 7677)."""
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.digest(salted, b"Client Key", "sha256")
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.digest(salted, b"Server Key", "sha256")

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def _psql(sql: str, database: str = "postgres") -> str:
    done = subprocess.run(
        [*_PSQL, "-d", database],
        input=sql,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise SystemExit(f"psql failed (exit {done.returncode}); no secret was sent")
    return done.stdout.strip()


def provision(profile: Profile) -> None:
    env_path = _REPO_ROOT / profile.env_file
    if env_path.exists():
        raise SystemExit(f"{profile.env_file} already exists; refusing to overwrite")
    exists = _psql(
        f"SELECT (SELECT count(*) FROM pg_roles WHERE rolname = '{profile.role}')"
        f" + (SELECT count(*) FROM pg_database WHERE datname = '{profile.database}');"
    )
    if exists != "0":
        raise SystemExit(f"role or database {profile.role!r} already exists; refusing")

    password = secrets.token_hex(24)
    verifier = scram_sha256_verifier(password, secrets.token_bytes(16))
    dsn = f"postgresql+psycopg2://{profile.role}:{password}@127.0.0.1:5432/{profile.database}"
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{profile.env_var}={dsn}\n")
    os.chmod(env_path, 0o600)
    try:
        _psql(
            f'CREATE ROLE "{profile.role}" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
            f"NOREPLICATION NOBYPASSRLS NOINHERIT CONNECTION LIMIT 32 PASSWORD '{verifier}';\n"
            f'CREATE DATABASE "{profile.database}" OWNER "{profile.role}" '
            f"ENCODING 'UTF8' TEMPLATE template0;\n"
            f'REVOKE ALL ON DATABASE "{profile.database}" FROM PUBLIC;\n'
        )
    except BaseException:
        env_path.unlink()
        raise
    print(f"provisioned role/database {profile.role}; credentials in {profile.env_file} (600)")


def main(argv: list[str]) -> None:
    if len(argv) != 1 or argv[0] not in PROFILES:
        raise SystemExit(f"usage: provision_local_postgres {{{'|'.join(PROFILES)}}}")
    provision(PROFILES[argv[0]])


if __name__ == "__main__":
    main(sys.argv[1:])
