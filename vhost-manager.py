#!/usr/bin/env python3
"""Command line manager for Homebrew Apache virtual hosts and certificates."""

from __future__ import annotations

import argparse
import contextlib
import io
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote, urlparse


APP_NAME = "vhost-manager"
BEGIN_MARKER = "# BEGIN vhost-manager: {name}"
END_MARKER = "# END vhost-manager: {name}"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VHOST_RE = re.compile(
    r"<VirtualHost\b(?P<listen>[^>]*)>(?P<body>.*?)</VirtualHost>",
    re.IGNORECASE | re.DOTALL,
)


class VhostManagerError(RuntimeError):
    """Expected error that can be shown without a traceback."""


@dataclass(frozen=True)
class Settings:
    vhosts_file: Path
    certs_dir: Path
    apachectl: Path
    hosts_file: Path


@dataclass(frozen=True)
class VhostRecord:
    """A VirtualHost entry ready to be displayed by the graphical interface."""

    name: str
    document_root: str
    aliases: tuple[str, ...]
    port: int
    ssl: bool
    cert_name: str
    managed: bool


def default_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / APP_NAME / "config.json"


def brew_prefix() -> Path:
    configured = os.environ.get("HOMEBREW_PREFIX")
    if configured:
        return Path(configured).expanduser()

    brew = shutil.which("brew")
    if brew:
        result = subprocess.run(
            [brew, "--prefix"], capture_output=True, text=True, check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())

    for candidate in (Path("/opt/homebrew"), Path("/usr/local")):
        if (candidate / "etc/httpd").exists():
            return candidate

    raise VhostManagerError(
        "Homebrew could not be detected. Configure the paths with "
        "'config set --vhosts-file ... --certs-dir ... --apachectl ...'."
    )


def read_config(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VhostManagerError(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise VhostManagerError(f"The configuration in {path} must be a JSON object.")
    return {key: str(value) for key, value in data.items() if value is not None}


def resolve_settings(config: dict[str, str]) -> Settings:
    required = {"vhosts_file", "certs_dir", "apachectl", "hosts_file"}
    if required.issubset(config):
        defaults: dict[str, Path] = {}
    else:
        prefix = brew_prefix()
        defaults = {
            "vhosts_file": prefix / "etc/httpd/extra/httpd-vhosts.conf",
            "certs_dir": prefix / "etc/httpd/certs",
            "apachectl": prefix / "bin/apachectl",
            "hosts_file": Path("/etc/hosts"),
        }

    def path_for(key: str, env_name: str) -> Path:
        value = os.environ.get(env_name) or config.get(key) or defaults[key]
        return Path(value).expanduser().resolve()

    return Settings(
        vhosts_file=path_for("vhosts_file", "VHOST_MANAGER_VHOSTS_FILE"),
        certs_dir=path_for("certs_dir", "VHOST_MANAGER_CERTS_DIR"),
        apachectl=path_for("apachectl", "VHOST_MANAGER_APACHECTL"),
        hosts_file=path_for("hosts_file", "VHOST_MANAGER_HOSTS_FILE"),
    )


def validate_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise VhostManagerError(
            f"Invalid name: {name!r}. Use letters, numbers, dots, hyphens, or '_'."
        )
    return name


def validate_directive_value(value: str, label: str) -> str:
    if "\n" in value or "\r" in value:
        raise VhostManagerError(f"{label} cannot contain line breaks.")
    return value


def valid_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("the port must be a number") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("the port must be between 1 and 65535")
    return port


def prompt_required(label: str, current: str | None) -> str:
    if current:
        return current
    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print(f"{label} is required.")


def prompt_yes_no(label: str, current: bool | None, default: bool = False) -> bool:
    if current is not None:
        return current
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input(f"{label} {suffix} ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def prompt_port(current: int | None, default: int) -> int:
    if current is not None:
        return current
    while True:
        answer = input(f"Port [{default}]: ").strip()
        if not answer:
            return default
        try:
            return valid_port(answer)
        except argparse.ArgumentTypeError as exc:
            print(f"Invalid value: {exc}")


def directive(body: str, name: str) -> str | None:
    match = re.search(
        rf"^\s*{re.escape(name)}\s+(.+?)\s*$", body, re.IGNORECASE | re.MULTILINE
    )
    return match.group(1).strip('"') if match else None


def directives(body: str, name: str) -> list[str]:
    return [
        match.group(1).strip().strip('"')
        for match in re.finditer(
            rf"^\s*{re.escape(name)}\s+(.+?)\s*$",
            body,
            re.IGNORECASE | re.MULTILINE,
        )
    ]


def parse_listen_port(listen: str, ssl: bool) -> int:
    match = re.search(r":(\d+)\s*$", listen.strip())
    if not match:
        return 443 if ssl else 80
    port = int(match.group(1))
    return port if 1 <= port <= 65535 else (443 if ssl else 80)


def parse_vhosts(content: str) -> list[VhostRecord]:
    """Parse both managed and hand-written vhosts without changing the file."""

    records: list[VhostRecord] = []
    for match in VHOST_RE.finditer(content):
        body = match.group("body")
        name = directive(body, "ServerName") or "(sin ServerName)"
        ssl = (directive(body, "SSLEngine") or "").lower() == "on"
        cert_path = directive(body, "SSLCertificateFile") or ""
        cert_name = Path(cert_path).stem if cert_path else name
        managed = False
        if name != "(sin ServerName)":
            managed = any(
                block.start() <= match.start() and block.end() >= match.end()
                for block in managed_block_pattern(name).finditer(content)
            )
        records.append(
            VhostRecord(
                name=name,
                document_root=directive(body, "DocumentRoot") or "",
                aliases=tuple(directives(body, "ServerAlias")),
                port=parse_listen_port(match.group("listen"), ssl),
                ssl=ssl,
                cert_name=cert_name,
                managed=managed,
            )
        )
    return records


def read_vhosts_file(settings: Settings) -> str:
    try:
        return settings.vhosts_file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise VhostManagerError(
            f"{settings.vhosts_file} does not exist. Correct the path with 'config set'."
        ) from exc
    except OSError as exc:
        raise VhostManagerError(f"Could not read {settings.vhosts_file}: {exc}") from exc


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o644

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    except OSError as exc:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise VhostManagerError(f"Could not write {path}: {exc}") from exc


def update_file(path: Path, old: str, new: str) -> Path:
    if old == new:
        raise VhostManagerError("There are no changes to save.")
    if not os.access(path, os.W_OK) or not os.access(path.parent, os.W_OK):
        return privileged_update_file(path, old, new)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.bak-{timestamp}")
    try:
        shutil.copy2(path, backup)
    except OSError as exc:
        raise VhostManagerError(f"Could not create backup {backup}: {exc}") from exc
    try:
        atomic_write(path, new)
    except Exception:
        shutil.copy2(backup, path)
        raise
    return backup


def privileged_update_file(path: Path, old: str, new: str) -> Path:
    """Install a protected file after requesting macOS administrator approval."""

    if sys.platform != "darwin":
        raise VhostManagerError(
            f"No permission to write {path}. Run the operation with suitable privileges."
        )
    osascript = Path("/usr/bin/osascript")
    installer = Path("/usr/bin/install")
    if not osascript.is_file() or not installer.is_file():
        raise VhostManagerError(f"No permission to write {path}.")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_dir = default_config_path().parent / "backups"
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{path.name}.bak-{timestamp}"
        backup.write_text(old, encoding="utf-8")
        mode = stat.S_IMODE(path.stat().st_mode)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=backup_dir
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(new)
            temporary.flush()
            os.fsync(temporary.fileno())
        staged = Path(temporary_name)
        staged.chmod(mode)
    except OSError as exc:
        raise VhostManagerError(
            f"Could not prepare the authorized update for {path}: {exc}"
        ) from exc

    command = " ".join(
        (
            shlex.quote(str(installer)),
            "-m",
            shlex.quote(f"{mode:o}"),
            shlex.quote(str(staged)),
            shlex.quote(str(path)),
        )
    )
    apple_command = command.replace("\\", "\\\\").replace('"', '\\"')
    script = f'do shell script "{apple_command}" with administrator privileges'
    try:
        result = subprocess.run(
            [str(osascript), "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        staged.unlink(missing_ok=True)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        if "canceled" in detail.lower() or "cancelled" in detail.lower():
            detail = "Authorization was cancelled."
        raise VhostManagerError(detail or f"Could not write {path}.")
    try:
        if path.read_text(encoding="utf-8") != new:
            raise VhostManagerError(f"The authorized update of {path} was not applied.")
    except OSError as exc:
        raise VhostManagerError(f"Could not verify {path}: {exc}") from exc
    return backup


def managed_block_pattern(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?m)^\s*{re.escape(BEGIN_MARKER.format(name=name))}\s*$.*?"
        rf"^\s*{re.escape(END_MARKER.format(name=name))}\s*$\n?",
        re.DOTALL,
    )


def read_hosts_file(settings: Settings) -> str:
    try:
        return settings.hosts_file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise VhostManagerError(
            f"{settings.hosts_file} does not exist. Correct the path with 'config set'."
        ) from exc
    except OSError as exc:
        raise VhostManagerError(f"Could not read {settings.hosts_file}: {exc}") from exc


def parsed_host_entries(content: str) -> list[tuple[str, list[str]]]:
    entries = []
    for line in content.splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) < 2:
            continue
        try:
            ipaddress.ip_address(fields[0])
        except ValueError:
            continue
        entries.append((fields[0], fields[1:]))
    return entries


def prepare_host_mapping(
    settings: Settings,
    name: str,
    aliases: Sequence[str],
    address: str,
    replace: bool,
) -> tuple[str, str]:
    name = validate_name(name)
    hostnames = list(dict.fromkeys([name, *(validate_name(alias) for alias in aliases)]))
    try:
        address = str(ipaddress.ip_address(address))
    except ValueError as exc:
        raise VhostManagerError(f"Invalid IP address: {address!r}.") from exc

    content = read_hosts_file(settings)
    original_content = content
    pattern = managed_block_pattern(name)
    managed = pattern.search(content)
    if managed and not replace:
        raise VhostManagerError(
            f"A managed hosts entry for {name} already exists; use --replace to replace it."
        )
    if managed:
        content = content[: managed.start()] + content[managed.end() :]

    configured_names = {
        hostname
        for _, names in parsed_host_entries(content)
        for hostname in names
    }
    conflicts = [hostname for hostname in hostnames if hostname in configured_names]
    if conflicts:
        raise VhostManagerError(
            "The following hostnames already have manual entries and will not be replaced: "
            + ", ".join(conflicts)
        )

    block = (
        f"{BEGIN_MARKER.format(name=name)}\n"
        f"{address}\t{' '.join(hostnames)}\n"
        f"{END_MARKER.format(name=name)}\n"
    )
    new_content = content.rstrip() + "\n\n" + block
    return original_content, new_content


def add_host_mapping(
    settings: Settings,
    name: str,
    aliases: Sequence[str],
    address: str,
    replace: bool,
) -> Path:
    old, new = prepare_host_mapping(settings, name, aliases, address, replace)
    return update_file(settings.hosts_file, old, new)


def host_list(_: argparse.Namespace, settings: Settings) -> None:
    entries = parsed_host_entries(read_hosts_file(settings))
    if not entries:
        print("No hosts entries were found.")
        return
    print(f"{'IP ADDRESS':39} HOSTNAMES")
    for address, hostnames in entries:
        print(f"{address:39} {' '.join(hostnames)}")


def host_add(args: argparse.Namespace, settings: Settings) -> None:
    backup = add_host_mapping(
        settings, args.name, args.alias, args.ip, args.replace
    )
    print(f"Hosts entry for {args.name} saved. Backup: {backup}")


def host_remove(args: argparse.Namespace, settings: Settings) -> None:
    name = validate_name(args.name)
    content = read_hosts_file(settings)
    match = managed_block_pattern(name).search(content)
    if not match:
        raise VhostManagerError(
            f"No managed hosts entry exists for {name}. Manual entries are not removed."
        )
    new_content = (content[: match.start()] + content[match.end() :]).rstrip() + "\n"
    backup = update_file(settings.hosts_file, content, new_content)
    print(f"Hosts entry for {name} removed. Backup: {backup}")


def has_managed_host_mapping(settings: Settings, name: str) -> bool:
    """Return whether /etc/hosts contains a block managed for this server name."""

    if not NAME_RE.fullmatch(name):
        return False
    return managed_block_pattern(name).search(read_hosts_file(settings)) is not None


def render_vhost(args: argparse.Namespace, settings: Settings) -> str:
    name = validate_name(args.name)
    validate_directive_value(args.document_root, "DocumentRoot")
    document_root = Path(args.document_root).expanduser().resolve()
    port = args.port if args.port is not None else (443 if args.ssl else 80)
    alias_names = [validate_name(alias) for alias in args.alias]
    aliases = "".join(f"\n    ServerAlias {alias}" for alias in alias_names)
    ssl = ""
    if args.ssl:
        cert_name = validate_name(args.cert_name or name)
        cert = settings.certs_dir / f"{cert_name}.crt"
        key = settings.certs_dir / f"{cert_name}.key"
        if not cert.is_file() or not key.is_file():
            generate_self_signed_certificate(
                settings=settings,
                cert_name=cert_name,
                common_name=name,
                sans=[name, *alias_names],
                days=825,
                force=True,
            )
        ssl = (
            "\n    SSLEngine on"
            f"\n    SSLCertificateFile \"{cert}\""
            f"\n    SSLCertificateKeyFile \"{key}\""
        )

    return (
        f"{BEGIN_MARKER.format(name=name)}\n"
        f"<VirtualHost *:{port}>\n"
        f"    ServerName {name}{aliases}\n"
        f"    DocumentRoot \"{document_root}\"{ssl}\n\n"
        f"    <Directory \"{document_root}\">\n"
        "        AllowOverride All\n"
        "        Require all granted\n"
        "    </Directory>\n"
        "</VirtualHost>\n"
        f"{END_MARKER.format(name=name)}\n"
    )


def config_show(args: argparse.Namespace, config: dict[str, str]) -> None:
    settings = resolve_settings(config)
    print(f"Config file:  {args.config}")
    print(f"Vhosts file: {settings.vhosts_file}")
    print(f"Certs dir:   {settings.certs_dir}")
    print(f"Apachectl:   {settings.apachectl}")
    print(f"Hosts file:  {settings.hosts_file}")


def config_set(args: argparse.Namespace, config: dict[str, str]) -> None:
    changes = {
        "vhosts_file": args.vhosts_file,
        "certs_dir": args.certs_dir,
        "apachectl": args.apachectl,
        "hosts_file": args.hosts_file,
    }
    if not any(changes.values()):
        raise VhostManagerError("Provide at least one path to save.")
    for key, value in changes.items():
        if value:
            config[key] = str(Path(value).expanduser().resolve())
    args.config.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(args.config, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    print(f"Configuration saved to {args.config}")


def vhost_list(_: argparse.Namespace, settings: Settings) -> None:
    content = read_vhosts_file(settings)
    matches = list(VHOST_RE.finditer(content))
    if not matches:
        print("No VirtualHost entries are configured.")
        return
    print(f"{'SERVER NAME':30} {'LISTEN':15} {'SSL':3} DOCUMENT ROOT")
    for match in matches:
        body = match.group("body")
        name = directive(body, "ServerName") or "-"
        root = directive(body, "DocumentRoot") or "-"
        ssl_enabled = (directive(body, "SSLEngine") or "").lower() == "on"
        ssl = "yes" if ssl_enabled else "no"
        print(f"{name:30} {match.group('listen').strip():15} {ssl:3} {root}")


def vhost_show(args: argparse.Namespace, settings: Settings) -> None:
    name = validate_name(args.name)
    content = read_vhosts_file(settings)
    blocks = [
        match.group(0)
        for match in VHOST_RE.finditer(content)
        if directive(match.group("body"), "ServerName") == name
    ]
    if not blocks:
        raise VhostManagerError(f"Vhost {name} was not found.")
    print("\n\n".join(blocks))


def vhost_add(args: argparse.Namespace, settings: Settings) -> None:
    content = read_vhosts_file(settings)
    original_content = content
    existing = [
        match
        for match in VHOST_RE.finditer(content)
        if directive(match.group("body"), "ServerName") == args.name
    ]
    managed = managed_block_pattern(args.name)
    if existing and not args.replace:
        raise VhostManagerError(
            f"A vhost for {args.name} already exists; use --replace to replace it."
        )
    if existing:
        match = managed.search(content)
        if not match:
            raise VhostManagerError(
                "The existing vhost was not created by this tool and will not be replaced."
            )
        content = content[: match.start()] + content[match.end() :]
    hosts_update = None
    if args.map_host:
        hosts_update = prepare_host_mapping(
            settings, args.name, args.alias, "127.0.0.1", replace=True
        )
    block = render_vhost(args, settings)
    new_content = content.rstrip() + "\n\n" + block
    backup = update_file(settings.vhosts_file, original_content, new_content)
    print(f"Vhost {args.name} saved. Backup: {backup}")
    if hosts_update:
        try:
            hosts_backup = update_file(
                settings.hosts_file, hosts_update[0], hosts_update[1]
            )
        except Exception:
            atomic_write(settings.vhosts_file, original_content)
            raise
        print(f"Mapped {args.name} to 127.0.0.1. Backup: {hosts_backup}")


def vhost_new(args: argparse.Namespace, settings: Settings) -> None:
    args.name = prompt_required("Server name", args.name)
    args.document_root = prompt_required("Document root", args.document_root)
    if args.alias is None:
        aliases = input("Server aliases (comma-separated, optional): ").strip()
        args.alias = [alias.strip() for alias in aliases.split(",") if alias.strip()]
    args.ssl = prompt_yes_no("Enable SSL?", args.ssl)
    args.port = prompt_port(args.port, 443 if args.ssl else 80)
    if args.ssl and not args.cert_name:
        cert_name = input(f"Certificate name [{args.name}]: ").strip()
        args.cert_name = cert_name or args.name
    args.map_host = prompt_yes_no(
        "Map the server name and aliases to 127.0.0.1?", args.map_host, default=True
    )
    vhost_add(args, settings)


def vhost_remove(args: argparse.Namespace, settings: Settings) -> None:
    name = validate_name(args.name)
    content = read_vhosts_file(settings)
    pattern = managed_block_pattern(name)
    match = pattern.search(content)
    if not match:
        raise VhostManagerError(
            f"No managed vhost exists for {name}. Manual blocks are not removed."
        )
    new_content = (content[: match.start()] + content[match.end() :]).rstrip() + "\n"
    backup = update_file(settings.vhosts_file, content, new_content)
    print(f"Vhost {name} removed. Backup: {backup}")


def run_apachectl(settings: Settings, *arguments: str) -> None:
    executable = settings.apachectl
    if not executable.is_file():
        raise VhostManagerError(f"apachectl does not exist at {executable}.")
    result = subprocess.run(
        [str(executable), *arguments], capture_output=True, text=True, check=False
    )
    output = (result.stdout + result.stderr).strip()
    if output:
        print(output)
    if result.returncode:
        raise VhostManagerError(
            f"apachectl exited with code {result.returncode}."
        )


def cert_paths(settings: Settings, name: str) -> tuple[Path, Path]:
    validate_name(name)
    return settings.certs_dir / f"{name}.crt", settings.certs_dir / f"{name}.key"


def cert_list(_: argparse.Namespace, settings: Settings) -> None:
    if not settings.certs_dir.exists():
        print(f"Directory {settings.certs_dir} does not exist.")
        return
    names = sorted(
        {path.stem for path in settings.certs_dir.glob("*.crt")}
        | {path.stem for path in settings.certs_dir.glob("*.key")}
    )
    if not names:
        print("No managed certificates were found.")
        return
    print(f"{'NAME':35} {'CERT':5} KEY")
    for name in names:
        cert, key = cert_paths(settings, name)
        print(f"{name:35} {'yes' if cert.is_file() else 'no':5} {'yes' if key.is_file() else 'no'}")


def stage_private_file(source: Path, destination: Path, mode: int) -> Path:
    if not source.is_file():
        raise VhostManagerError(f"Source file {source} does not exist.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(dir=destination.parent)
        os.close(descriptor)
        staged = Path(temporary_name)
        shutil.copyfile(source, staged)
        staged.chmod(mode)
        return staged
    except OSError as exc:
        if staged:
            staged.unlink(missing_ok=True)
        raise VhostManagerError(f"Could not copy {destination}: {exc}") from exc


def cert_install(args: argparse.Namespace, settings: Settings) -> None:
    cert, key = cert_paths(settings, args.name)
    source_cert = Path(args.cert).expanduser()
    source_key = Path(args.key).expanduser()
    for source in (source_cert, source_key):
        if not source.is_file():
            raise VhostManagerError(f"Source file {source} does not exist.")
    existing = [path for path in (cert, key) if path.exists()]
    if existing and not args.force:
        raise VhostManagerError(
            f"{existing[0]} already exists; use --force to replace the pair."
        )

    staged_cert = stage_private_file(source_cert, cert, 0o644)
    try:
        staged_key = stage_private_file(source_key, key, 0o600)
    except Exception:
        staged_cert.unlink(missing_ok=True)
        raise
    try:
        os.replace(staged_cert, cert)
        os.replace(staged_key, key)
    finally:
        staged_cert.unlink(missing_ok=True)
        staged_key.unlink(missing_ok=True)
    print(f"Certificate installed at {cert}; private key installed at {key}")


def generate_self_signed_certificate(
    settings: Settings,
    cert_name: str,
    common_name: str,
    sans: Sequence[str],
    days: int,
    force: bool,
) -> tuple[Path, Path]:
    cert, key = cert_paths(settings, cert_name)
    common_name = validate_name(common_name)
    if days < 1:
        raise VhostManagerError("--days must be a positive number.")
    if (cert.exists() or key.exists()) and not force:
        raise VhostManagerError("The certificate already exists; use --force to replace it.")
    openssl = shutil.which("openssl")
    if not openssl:
        raise VhostManagerError("openssl was not found in PATH.")
    settings.certs_dir.mkdir(parents=True, exist_ok=True)
    san_names = list(dict.fromkeys(validate_name(name) for name in sans))
    with tempfile.TemporaryDirectory(dir=settings.certs_dir) as temporary:
        temp_cert = Path(temporary) / "certificate.crt"
        temp_key = Path(temporary) / "private.key"
        command = [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
            "-days", str(days), "-keyout", str(temp_key), "-out", str(temp_cert),
            "-subj", f"/CN={common_name}", "-addext",
            "subjectAltName=" + ",".join(f"DNS:{name}" for name in san_names),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise VhostManagerError(result.stderr.strip() or "openssl failed.")
        os.replace(temp_cert, cert)
        os.replace(temp_key, key)
    cert.chmod(0o644)
    key.chmod(0o600)
    print(f"Self-signed certificate generated at {cert}")
    return cert, key


def cert_generate(args: argparse.Namespace, settings: Settings) -> None:
    generate_self_signed_certificate(
        settings=settings,
        cert_name=args.name,
        common_name=args.name,
        sans=[args.name, *args.san],
        days=args.days,
        force=args.force,
    )


def cert_remove(args: argparse.Namespace, settings: Settings) -> None:
    cert, key = cert_paths(settings, args.name)
    existing = [path for path in (cert, key) if path.exists()]
    if not existing:
        raise VhostManagerError(f"No certificate exists for {args.name}.")
    if not args.yes:
        answer = input(f"Remove the certificate and key for {args.name}? [y/N] ").lower()
        if answer not in {"y", "yes"}:
            print("Operation cancelled.")
            return
    for path in existing:
        path.unlink()
    print(f"Certificate {args.name} removed.")


def launch_web_gui(config_path: Path, config: dict[str, str]) -> None:
    """Serve the graphical manager locally and open it in the default browser."""

    token = secrets.token_urlsafe(24)
    html = r"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Vhost Manager</title>
  <style>
    :root { color-scheme: light; --bg:#f4f5f7; --panel:#fff; --line:#dfe3e8;
      --text:#18202a; --muted:#667085; --blue:#1769e0; --danger:#b42318; }
    * { box-sizing:border-box } body { margin:0; font:14px -apple-system,BlinkMacSystemFont,
      "Segoe UI",sans-serif; color:var(--text); background:var(--bg) }
    header { height:68px; display:flex; align-items:center; justify-content:space-between;
      padding:0 28px; background:#111827; color:#fff }
    header h1 { margin:0; font-size:21px } header span { color:#aeb8c7; font-size:12px }
    main { max-width:1400px; margin:24px auto; padding:0 24px; display:grid;
      grid-template-columns:minmax(520px,1.55fr) minmax(360px,1fr); gap:20px }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:12px;
      box-shadow:0 4px 14px rgba(16,24,40,.05); overflow:hidden }
    .panel-title { display:flex; align-items:center; justify-content:space-between;
      padding:18px 20px; border-bottom:1px solid var(--line) }
    .panel-title h2 { margin:0; font-size:16px } button { border:0; border-radius:7px;
      padding:9px 13px; font-weight:600; cursor:pointer; background:#e9edf3; color:#253044 }
    button.primary { background:var(--blue); color:#fff } button.danger { color:var(--danger);
      background:#feeceb } button:disabled { opacity:.45; cursor:not-allowed }
    .table-wrap { overflow:auto; min-height:420px } table { border-collapse:collapse; width:100% }
    th { text-align:left; padding:11px 14px; font-size:11px; text-transform:uppercase;
      letter-spacing:.05em; color:var(--muted); background:#fafbfc; border-bottom:1px solid var(--line) }
    td { padding:13px 14px; border-bottom:1px solid #edf0f3; white-space:nowrap }
    tr[data-id] { cursor:pointer } tr[data-id]:hover, tr.selected { background:#eef5ff }
    td.root { max-width:280px; overflow:hidden; text-overflow:ellipsis }
    .badge { display:inline-block; padding:3px 7px; border-radius:10px; font-size:11px;
      background:#e9edf3; color:#475467 }.badge.managed { background:#e8f7ee; color:#18794e }
    form { padding:20px } label { display:block; margin-bottom:14px; color:#475467;
      font-size:12px; font-weight:600 } input { display:block; width:100%; margin-top:6px;
      border:1px solid #cfd6df; border-radius:7px; padding:10px 11px; font:inherit; color:var(--text) }
    input:focus { outline:2px solid #b9d3ff; border-color:var(--blue) }
    .row { display:grid; grid-template-columns:1fr 1fr; gap:12px }
    .check { display:flex; align-items:center; gap:8px; font-size:13px; font-weight:500 }
    .check input { width:auto; margin:0 }.actions { display:flex; gap:9px; margin-top:20px }
    .actions .danger { margin-left:auto }.notice { min-height:38px; margin:2px 0 12px;
      padding:10px 12px; border-radius:7px; background:#f5f7fa; color:var(--muted); font-size:12px }
    #status { padding:12px 20px; border-top:1px solid var(--line); color:var(--muted); font-size:12px }
    #toast { position:fixed; right:24px; bottom:24px; padding:12px 16px; border-radius:8px;
      color:#fff; background:#223046; box-shadow:0 6px 20px #0003; opacity:0;
      transform:translateY(10px); transition:.2s; pointer-events:none }
    #toast.show { opacity:1; transform:none } #toast.error { background:#b42318 }
    @media(max-width:900px) { main { grid-template-columns:1fr } }
  </style>
</head>
<body>
<header><h1>Vhost Manager</h1><span>Gestión local de Apache</span></header>
<main>
  <section class="panel">
    <div class="panel-title"><h2>Virtual hosts</h2><button id="refresh">Recargar</button></div>
    <div class="table-wrap"><table><thead><tr><th>Servidor</th><th>Puerto</th><th>SSL</th>
      <th>Directorio</th><th>Tipo</th></tr></thead><tbody id="list"></tbody></table></div>
    <div id="status">Cargando…</div>
  </section>
  <section class="panel">
    <div class="panel-title"><h2 id="form-title">Nuevo vhost</h2><button id="new">Nuevo</button></div>
    <form id="form">
      <label>ServerName<input id="name" required placeholder="proyecto.test"></label>
      <label>DocumentRoot<input id="document_root" required placeholder="/ruta/al/public"></label>
      <label>Aliases, separados por comas<input id="aliases" placeholder="www.proyecto.test"></label>
      <div class="row"><label>Puerto<input id="port" type="number" min="1" max="65535" value="80" required></label>
        <label>Certificado<input id="cert_name" placeholder="proyecto.test" disabled></label></div>
      <div class="row"><label class="check"><input id="ssl" type="checkbox"> Activar SSL</label>
        <label class="check"><input id="map_host" type="checkbox" checked> Gestionar en /etc/hosts</label></div>
      <div class="notice" id="notice">Completa los datos para crear un vhost gestionado.</div>
      <div class="actions"><button class="primary" id="save" type="submit">Guardar</button>
        <button class="danger" id="delete" type="button" disabled>Eliminar</button></div>
    </form>
  </section>
</main><div id="toast"></div>
<script>
const TOKEN = __TOKEN__;
let records = [], selected = null;
const $ = id => document.getElementById(id);
const fields = ['name','document_root','aliases','port','cert_name','ssl','map_host'];
function toast(message, error=false) { const el=$('toast'); el.textContent=message;
  el.className='show'+(error?' error':''); clearTimeout(el.timer);
  el.timer=setTimeout(()=>el.className='',2800); }
function esc(value) { const node=document.createElement('span'); node.textContent=value ?? ''; return node.innerHTML; }
async function request(path, options={}) { options.headers={...(options.headers||{}),'X-Vhost-Token':TOKEN};
  const response=await fetch(path,options); const data=await response.json();
  if(!response.ok) throw new Error(data.error||'Error inesperado'); return data; }
async function load() { try { const data=await request('/api/vhosts'); records=data.vhosts;
    $('status').textContent=`${records.length} vhost(s) · ${data.path}`;
    $('list').innerHTML=records.map((v,i)=>`<tr data-id="${i}"><td><strong>${esc(v.name)}</strong></td>
      <td>${v.port}</td><td>${v.ssl?'Sí':'No'}</td><td class="root" title="${esc(v.document_root)}">${esc(v.document_root)}</td>
      <td><span class="badge ${v.managed?'managed':''}">${v.managed?'Gestionado':'Manual'}</span></td></tr>`).join('');
    document.querySelectorAll('tr[data-id]').forEach(row=>row.onclick=()=>select(Number(row.dataset.id)));
    if(selected!==null) { const index=records.findIndex(v=>v.name===selected); index>=0?select(index):reset(); }
  } catch(error) { $('status').textContent='Error: '+error.message; toast(error.message,true); } }
function setDisabled(disabled) { fields.forEach(id=>$(id).disabled=disabled);
  $('save').disabled=disabled; $('delete').disabled=disabled||selected===null; }
function select(index) { const v=records[index]; selected=v.name;
  document.querySelectorAll('tr[data-id]').forEach((r,i)=>r.classList.toggle('selected',i===index));
  $('form-title').textContent=v.name; $('name').value=v.name; $('document_root').value=v.document_root;
  $('aliases').value=v.aliases.join(', '); $('port').value=v.port; $('ssl').checked=v.ssl;
  $('cert_name').value=v.cert_name; $('map_host').checked=v.host_mapped; setDisabled(!v.managed);
  if(v.managed) { $('save').textContent='Guardar'; $('name').disabled=true; $('cert_name').disabled=!v.ssl;
    $('notice').textContent='Al guardar se creará una copia de respaldo.'; }
  else { $('map_host').disabled=false; $('save').disabled=false; $('save').textContent='Guardar hosts';
    $('notice').textContent='Vhost manual: solo puedes gestionar su entrada en /etc/hosts.'; } }
function reset() { selected=null; $('form').reset(); $('port').value=80; $('map_host').checked=true;
  $('form-title').textContent='Nuevo vhost'; setDisabled(false); $('cert_name').disabled=true;
  $('save').textContent='Guardar'; $('delete').disabled=true; $('notice').textContent='Completa los datos para crear un vhost gestionado.';
  document.querySelectorAll('tr.selected').forEach(r=>r.classList.remove('selected')); }
$('ssl').onchange=()=>{ $('cert_name').disabled=!$('ssl').checked;
  if($('ssl').checked&&!$('cert_name').value) $('cert_name').value=$('name').value; };
$('new').onclick=reset; $('refresh').onclick=load;
$('form').onsubmit=async event=>{ event.preventDefault(); const body={original_name:selected,
  name:$('name').value.trim(),document_root:$('document_root').value.trim(),
  aliases:$('aliases').value.split(',').map(v=>v.trim()).filter(Boolean),port:Number($('port').value),
  ssl:$('ssl').checked,cert_name:$('cert_name').value.trim()||null,map_host:$('map_host').checked};
  const current=selected===null?null:records.find(v=>v.name===selected);
  const endpoint=current&&!current.managed?'/api/hosts':'/api/vhosts';
  try { await request(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    selected=body.name; await load(); toast('Cambios guardados'); } catch(error) { toast(error.message,true); } };
$('delete').onclick=async()=>{ if(!selected||!confirm(`¿Eliminar ${selected}? Se conservará una copia de respaldo.`)) return;
  try { await request('/api/vhosts/'+encodeURIComponent(selected),{method:'DELETE'}); reset(); await load(); toast('Vhost eliminado'); }
  catch(error) { toast(error.message,true); } };
reset(); load();
</script></body></html>""".replace("__TOKEN__", json.dumps(token))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def send_json(self, status: int, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Vhost-Token", ""), token)

        def settings(self) -> Settings:
            return resolve_settings(config)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/vhosts" and self.authorized():
                try:
                    settings = self.settings()
                    records = parse_vhosts(read_vhosts_file(settings))
                    payload = [
                        {
                            "name": item.name,
                            "document_root": item.document_root,
                            "aliases": item.aliases,
                            "port": item.port,
                            "ssl": item.ssl,
                            "cert_name": item.cert_name,
                            "managed": item.managed,
                            "host_mapped": has_managed_host_mapping(
                                settings, item.name
                            ),
                        }
                        for item in records
                    ]
                    self.send_json(200, {"vhosts": payload, "path": str(settings.vhosts_file)})
                except (VhostManagerError, OSError) as exc:
                    self.send_json(400, {"error": str(exc)})
                return
            self.send_json(404, {"error": "No encontrado"})

        def read_payload(self) -> dict[str, object]:
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 65536)
                value = json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError) as exc:
                raise VhostManagerError("La petición no contiene JSON válido.") from exc
            if not isinstance(value, dict):
                raise VhostManagerError("La petición debe ser un objeto JSON.")
            return value

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path not in {"/api/vhosts", "/api/hosts"} or not self.authorized():
                self.send_json(403, {"error": "Petición no autorizada"})
                return
            try:
                data = self.read_payload()
                name = str(data.get("name", "")).strip()
                original = str(data.get("original_name") or "").strip()
                aliases = data.get("aliases", [])
                if not isinstance(aliases, list):
                    raise VhostManagerError("Los aliases no son válidos.")
                alias_names = [
                    str(alias).strip() for alias in aliases if str(alias).strip()
                ]
                map_host = bool(data.get("map_host"))

                if path == "/api/hosts":
                    settings = self.settings()
                    with contextlib.redirect_stdout(io.StringIO()):
                        if map_host:
                            add_host_mapping(
                                settings,
                                name,
                                alias_names,
                                "127.0.0.1",
                                replace=True,
                            )
                        elif has_managed_host_mapping(settings, name):
                            host_remove(argparse.Namespace(name=name), settings)
                    self.send_json(200, {"ok": True})
                    return

                if original and name != original:
                    raise VhostManagerError(
                        "Para cambiar ServerName, crea un vhost nuevo y elimina el anterior."
                    )
                document_root = str(data.get("document_root", "")).strip()
                if not document_root:
                    raise VhostManagerError("DocumentRoot es obligatorio.")
                args = argparse.Namespace(
                    name=name,
                    document_root=document_root,
                    alias=alias_names,
                    port=valid_port(str(data.get("port", ""))),
                    ssl=bool(data.get("ssl")),
                    cert_name=str(data.get("cert_name") or "").strip() or None,
                    map_host=map_host,
                    replace=bool(original),
                )
                settings = self.settings()
                with contextlib.redirect_stdout(io.StringIO()):
                    vhost_add(args, settings)
                    if (
                        original
                        and not map_host
                        and has_managed_host_mapping(settings, name)
                    ):
                        host_remove(argparse.Namespace(name=name), settings)
                self.send_json(200, {"ok": True})
            except (VhostManagerError, OSError, argparse.ArgumentTypeError) as exc:
                self.send_json(400, {"error": str(exc)})

        def do_DELETE(self) -> None:
            path = urlparse(self.path).path
            if not path.startswith("/api/vhosts/") or not self.authorized():
                self.send_json(403, {"error": "Petición no autorizada"})
                return
            try:
                name = unquote(path.removeprefix("/api/vhosts/"))
                with contextlib.redirect_stdout(io.StringIO()):
                    vhost_remove(argparse.Namespace(name=name), self.settings())
                self.send_json(200, {"ok": True})
            except (VhostManagerError, OSError) as exc:
                self.send_json(400, {"error": str(exc)})

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except OSError as exc:
        raise VhostManagerError(f"No se pudo iniciar la interfaz local: {exc}") from exc
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Vhost Manager abierto en {url}")
    print("Pulsa Ctrl+C para cerrarlo.")
    if not webbrowser.open(url, new=1):
        print(f"Abre esta dirección en tu navegador: {url}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def launch_gui(config_path: Path, config: dict[str, str]) -> None:
    """Open the desktop manager. Tk is imported lazily so the CLI stays portable."""

    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:
        raise VhostManagerError(
            "The graphical interface requires Tkinter (the Python 'tk' package)."
        ) from exc

    class VhostManagerGUI:
        def __init__(self) -> None:
            self.root = tk.Tk()
            self.root.withdraw()
            self.root.title("Vhost Manager")
            self.root.geometry("1060x650")
            self.root.minsize(880, 560)
            self.root.report_callback_exception = self._report_callback_exception
            self.config = dict(config)
            self.settings: Settings | None = None
            self.records: dict[str, VhostRecord] = {}
            self.editing_name: str | None = None
            self.editor_enabled = False
            self.editor_widgets: list[object] = []

            style = ttk.Style(self.root)
            if "aqua" in style.theme_names():
                style.theme_use("aqua")
            style.configure("Title.TLabel", font=("TkDefaultFont", 18, "bold"))
            style.configure("Muted.TLabel", foreground="#666666")
            style.configure("Danger.TButton", foreground="#a51d1d")

            self._build()
            self.root.createcommand("tk::mac::ReopenApplication", self._show_window)
            self.root.after_idle(self._show_window)
            self.root.after(50, self.reload)

        def _show_window(self) -> None:
            self.root.deiconify()
            self.root.lift()
            self.root.update_idletasks()

        def _report_callback_exception(self, exc_type, exc_value, _traceback) -> None:
            message = f"{exc_type.__name__}: {exc_value}"
            self.status_var.set(f"Error: {message}")
            messagebox.showerror(
                "Error en Vhost Manager",
                f"La interfaz no pudo completar una operación:\n\n{message}",
                parent=self.root,
            )

        def _build(self) -> None:
            outer = ttk.Frame(self.root, padding=18)
            outer.pack(fill="both", expand=True)

            header = ttk.Frame(outer)
            header.pack(fill="x", pady=(0, 14))
            ttk.Label(header, text="Vhosts de Apache", style="Title.TLabel").pack(
                side="left"
            )
            ttk.Button(header, text="Configuración", command=self.open_settings).pack(
                side="right"
            )
            ttk.Button(header, text="Recargar", command=self.reload).pack(
                side="right", padx=(0, 8)
            )

            pane = ttk.Panedwindow(outer, orient="horizontal")
            pane.pack(fill="both", expand=True)

            left = ttk.Frame(pane, padding=(0, 0, 12, 0))
            right = ttk.Frame(pane, padding=(14, 4, 2, 2))
            pane.add(left, weight=3)
            pane.add(right, weight=2)

            columns = ("name", "listen", "ssl", "root", "type")
            self.tree = ttk.Treeview(
                left, columns=columns, show="headings", selectmode="browse"
            )
            headings = {
                "name": "Servidor",
                "listen": "Puerto",
                "ssl": "SSL",
                "root": "Directorio",
                "type": "Tipo",
            }
            widths = {"name": 170, "listen": 60, "ssl": 45, "root": 250, "type": 85}
            for column in columns:
                self.tree.heading(column, text=headings[column])
                self.tree.column(column, width=widths[column], minwidth=45)
            scrollbar = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=scrollbar.set)
            self.tree.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")
            self.tree.bind("<<TreeviewSelect>>", self.on_select)

            ttk.Label(right, text="Detalles", font=("TkDefaultFont", 13, "bold")).grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
            )
            self.name_var = tk.StringVar()
            self.root_var = tk.StringVar()
            self.alias_var = tk.StringVar()
            self.port_var = tk.StringVar(value="80")
            self.ssl_var = tk.BooleanVar()
            self.cert_var = tk.StringVar()
            self.map_var = tk.BooleanVar(value=True)
            self.notice_var = tk.StringVar(value="Selecciona un vhost o crea uno nuevo.")

            self.name_entry = self._field(right, 1, "ServerName", self.name_var)
            self._field(right, 2, "DocumentRoot", self.root_var, browse=True)
            self._field(right, 3, "Alias (separados por comas)", self.alias_var)
            self._field(right, 4, "Puerto", self.port_var)
            self.ssl_check = ttk.Checkbutton(
                right, text="Activar SSL", variable=self.ssl_var, command=self._ssl_changed
            )
            self.ssl_check.grid(row=5, column=1, sticky="w", pady=5)
            self.editor_widgets.append(self.ssl_check)
            self.cert_label = ttk.Label(right, text="Certificado")
            self.cert_label.grid(row=6, column=0, sticky="w", pady=5)
            self.cert_entry = ttk.Entry(right, textvariable=self.cert_var)
            self.cert_entry.grid(row=6, column=1, columnspan=2, sticky="ew", pady=5)
            self.editor_widgets.append(self.cert_entry)
            self.map_check = ttk.Checkbutton(
                right,
                text="Añadir nombres a /etc/hosts al crear",
                variable=self.map_var,
            )
            self.map_check.grid(row=7, column=1, columnspan=2, sticky="w", pady=5)
            self.editor_widgets.append(self.map_check)
            ttk.Label(
                right,
                textvariable=self.notice_var,
                style="Muted.TLabel",
                wraplength=330,
                justify="left",
            ).grid(row=8, column=0, columnspan=3, sticky="ew", pady=(12, 8))

            buttons = ttk.Frame(right)
            buttons.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(8, 0))
            self.new_button = ttk.Button(buttons, text="Nuevo", command=self.new_vhost)
            self.new_button.pack(side="left")
            self.save_button = ttk.Button(buttons, text="Guardar", command=self.save)
            self.save_button.pack(side="left", padx=8)
            self.delete_button = ttk.Button(
                buttons, text="Eliminar", style="Danger.TButton", command=self.delete
            )
            self.delete_button.pack(side="right")
            right.columnconfigure(1, weight=1)
            self._ssl_changed()
            self._set_editor_state(False)

            self.status_var = tk.StringVar(value="Preparando…")
            ttk.Label(outer, textvariable=self.status_var, style="Muted.TLabel").pack(
                fill="x", pady=(10, 0)
            )
            self.new_vhost(clear_selection=False)

        def _field(self, parent, row: int, label: str, variable, browse: bool = False):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5)
            entry = ttk.Entry(parent, textvariable=variable)
            entry.grid(row=row, column=1, sticky="ew", pady=5)
            self.editor_widgets.append(entry)
            if browse:
                button = ttk.Button(parent, text="…", width=3, command=self.choose_root)
                button.grid(row=row, column=2, padx=(6, 0), pady=5)
                self.editor_widgets.append(button)
            return entry

        def _set_editor_state(self, enabled: bool, manual: bool = False) -> None:
            state = "normal" if enabled and not manual else "disabled"
            self.editor_enabled = state == "normal"
            for widget in self.editor_widgets:
                widget.configure(state=state)
            if self.editor_enabled and not self.ssl_var.get():
                self.cert_entry.configure(state="disabled")
            if self.editor_enabled and self.editing_name:
                self.name_entry.configure(state="disabled")
                self.map_check.configure(state="disabled")
            self.save_button.configure(state=state)
            self.delete_button.configure(
                state="normal" if enabled and not manual and self.editing_name else "disabled"
            )

        def _ssl_changed(self) -> None:
            state = "normal" if self.editor_enabled and self.ssl_var.get() else "disabled"
            self.cert_entry.configure(state=state)
            if self.ssl_var.get() and not self.cert_var.get().strip():
                self.cert_var.set(self.name_var.get().strip())

        def choose_root(self) -> None:
            selected = filedialog.askdirectory(
                parent=self.root,
                initialdir=self.root_var.get() or str(Path.home()),
                title="Seleccionar DocumentRoot",
            )
            if selected:
                self.root_var.set(selected)

        def _resolve_settings(self) -> bool:
            try:
                self.settings = resolve_settings(self.config)
                return True
            except VhostManagerError as exc:
                self.settings = None
                self.status_var.set(str(exc))
                if messagebox.askyesno(
                    "Falta configuración",
                    f"{exc}\n\n¿Quieres configurar ahora las rutas?",
                    parent=self.root,
                ):
                    self.open_settings()
                return False

        def reload(self) -> None:
            if not self._resolve_settings():
                return
            assert self.settings is not None
            try:
                records = parse_vhosts(read_vhosts_file(self.settings))
            except (VhostManagerError, OSError) as exc:
                self.status_var.set(f"Error: {exc}")
                messagebox.showerror("No se pueden leer los vhosts", str(exc), parent=self.root)
                return
            selected = self.editing_name
            self.records = {}
            self.tree.delete(*self.tree.get_children())
            for index, record in enumerate(records):
                item_id = f"vhost-{index}"
                self.records[item_id] = record
                item = self.tree.insert(
                    "",
                    "end",
                    iid=item_id,
                    values=(
                        record.name,
                        record.port,
                        "Sí" if record.ssl else "No",
                        record.document_root,
                        "Gestionado" if record.managed else "Manual",
                    ),
                )
                if selected == record.name:
                    self.tree.selection_set(item)
            self.status_var.set(
                f"{len(records)} vhost(s) · {self.settings.vhosts_file}"
            )
            if selected is None or not any(
                record.name == selected for record in records
            ):
                self.new_vhost(clear_selection=False)

        def on_select(self, _event=None) -> None:
            selected = self.tree.selection()
            if not selected:
                return
            record = self.records[selected[0]]
            self.editing_name = record.name
            self.name_var.set(record.name)
            self.root_var.set(record.document_root)
            self.alias_var.set(", ".join(record.aliases))
            self.port_var.set(str(record.port))
            self.ssl_var.set(record.ssl)
            self.cert_var.set(record.cert_name)
            self.map_var.set(False)
            self._ssl_changed()
            self._set_editor_state(True, manual=not record.managed)
            if record.managed:
                self.notice_var.set(
                    "Al guardar se creará una copia de respaldo del archivo de vhosts."
                )
            else:
                self.notice_var.set(
                    "Este bloque no fue creado por Vhost Manager. Se muestra en solo lectura para no sobrescribir configuración manual."
                )

        def new_vhost(self, clear_selection: bool = True) -> None:
            if clear_selection:
                self.tree.selection_remove(self.tree.selection())
            self.editing_name = None
            self.name_var.set("")
            self.root_var.set("")
            self.alias_var.set("")
            self.port_var.set("80")
            self.ssl_var.set(False)
            self.cert_var.set("")
            self.map_var.set(True)
            self._ssl_changed()
            self._set_editor_state(True)
            self.notice_var.set("Completa los datos para crear un vhost gestionado.")

        def _aliases(self) -> list[str]:
            return [
                value.strip()
                for value in re.split(r"[,\n]", self.alias_var.get())
                if value.strip()
            ]

        def save(self) -> None:
            if not self._resolve_settings():
                return
            name = self.name_var.get().strip()
            document_root = self.root_var.get().strip()
            if not name or not document_root:
                messagebox.showwarning(
                    "Faltan datos", "ServerName y DocumentRoot son obligatorios.", parent=self.root
                )
                return
            if self.editing_name and name != self.editing_name:
                messagebox.showwarning(
                    "ServerName no editable",
                    "Para cambiar el nombre, crea un vhost nuevo y elimina el anterior.",
                    parent=self.root,
                )
                return
            try:
                port = valid_port(self.port_var.get().strip())
                args = argparse.Namespace(
                    name=name,
                    document_root=document_root,
                    alias=self._aliases(),
                    port=port,
                    ssl=self.ssl_var.get(),
                    cert_name=self.cert_var.get().strip() or None,
                    map_host=self.map_var.get() if self.editing_name is None else False,
                    replace=self.editing_name is not None,
                )
                assert self.settings is not None
                vhost_add(args, self.settings)
            except (VhostManagerError, OSError, argparse.ArgumentTypeError) as exc:
                messagebox.showerror("No se pudo guardar", str(exc), parent=self.root)
                return
            self.editing_name = name
            self.reload()
            messagebox.showinfo("Vhost guardado", f"{name} se ha guardado correctamente.", parent=self.root)

        def delete(self) -> None:
            if not self.editing_name or not self._resolve_settings():
                return
            name = self.editing_name
            if not messagebox.askyesno(
                "Eliminar vhost",
                f"¿Eliminar el vhost gestionado {name}?\n\nSe conservará una copia de respaldo.",
                parent=self.root,
            ):
                return
            try:
                assert self.settings is not None
                vhost_remove(argparse.Namespace(name=name), self.settings)
            except (VhostManagerError, OSError) as exc:
                messagebox.showerror("No se pudo eliminar", str(exc), parent=self.root)
                return
            self.editing_name = None
            self.reload()

        def test_apache(self) -> None:
            if not self._resolve_settings():
                return
            try:
                assert self.settings is not None
                run_apachectl(self.settings, "configtest")
            except (VhostManagerError, OSError) as exc:
                messagebox.showerror("Configuración inválida", str(exc), parent=self.root)
            else:
                messagebox.showinfo("Apache", "La configuración es válida.", parent=self.root)

        def open_settings(self) -> None:
            window = tk.Toplevel(self.root)
            window.title("Configuración")
            window.transient(self.root)
            window.grab_set()
            frame = ttk.Frame(window, padding=18)
            frame.pack(fill="both", expand=True)
            keys = (
                ("vhosts_file", "Archivo de vhosts"),
                ("certs_dir", "Directorio de certificados"),
                ("apachectl", "Ejecutable apachectl"),
                ("hosts_file", "Archivo hosts"),
            )
            variables: dict[str, tk.StringVar] = {}
            for row, (key, label) in enumerate(keys):
                ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=6)
                variable = tk.StringVar(value=self.config.get(key, ""))
                variables[key] = variable
                ttk.Entry(frame, textvariable=variable, width=58).grid(
                    row=row, column=1, sticky="ew", padx=(12, 0), pady=6
                )

            def save_settings() -> None:
                values = {key: value.get().strip() for key, value in variables.items()}
                if not all(values.values()):
                    messagebox.showwarning(
                        "Faltan rutas", "Completa las cuatro rutas.", parent=window
                    )
                    return
                self.config.update(
                    {key: str(Path(value).expanduser().resolve()) for key, value in values.items()}
                )
                try:
                    config_path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(
                        config_path,
                        json.dumps(self.config, indent=2, ensure_ascii=False) + "\n",
                    )
                except (VhostManagerError, OSError) as exc:
                    messagebox.showerror("No se pudo guardar", str(exc), parent=window)
                    return
                window.destroy()
                self.reload()

            actions = ttk.Frame(frame)
            actions.grid(row=len(keys), column=0, columnspan=2, sticky="ew", pady=(14, 0))
            ttk.Button(actions, text="Probar Apache", command=self.test_apache).pack(side="left")
            ttk.Button(actions, text="Cancelar", command=window.destroy).pack(side="right")
            ttk.Button(actions, text="Guardar", command=save_settings).pack(
                side="right", padx=(0, 8)
            )
            frame.columnconfigure(1, weight=1)

        def run(self) -> None:
            self.root.mainloop()

    VhostManagerGUI().run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description=(
            "Manage vhosts, local host mappings, and certificates for Apache "
            "installed with Homebrew."
        ),
    )
    parser.add_argument(
        "--config", type=Path, default=default_config_path(),
        help="JSON configuration file (default: %(default)s)",
    )
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("gui", help="open the graphical vhost manager in the browser")
    commands.add_parser("desktop", help="open the experimental Tk desktop interface")

    config = commands.add_parser("config", help="view or change paths")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("show", help="show the effective configuration")
    config_set_parser = config_commands.add_parser("set", help="save alternative paths")
    config_set_parser.add_argument("--vhosts-file")
    config_set_parser.add_argument("--certs-dir")
    config_set_parser.add_argument("--apachectl")
    config_set_parser.add_argument("--hosts-file")

    vhost = commands.add_parser("vhost", help="manage Apache VirtualHost entries")
    vhost_commands = vhost.add_subparsers(dest="vhost_command", required=True)
    vhost_commands.add_parser("list", help="list vhosts")
    vhost_show_parser = vhost_commands.add_parser("show", help="show a vhost")
    vhost_show_parser.add_argument("name")
    vhost_add_parser = vhost_commands.add_parser("add", help="create a vhost")
    vhost_add_parser.add_argument("name", help="vhost ServerName")
    vhost_add_parser.add_argument("--document-root", required=True)
    vhost_add_parser.add_argument("--alias", action="append", default=[])
    vhost_add_parser.add_argument("--port", type=valid_port)
    vhost_add_parser.add_argument("--ssl", action="store_true")
    vhost_add_parser.add_argument("--cert-name")
    vhost_add_parser.add_argument(
        "--map-host", action="store_true", help="map the vhost names to 127.0.0.1"
    )
    vhost_add_parser.add_argument("--replace", action="store_true")
    vhost_new_parser = vhost_commands.add_parser(
        "new", help="interactively create a vhost"
    )
    vhost_new_parser.add_argument("name", nargs="?", help="vhost ServerName")
    vhost_new_parser.add_argument("--document-root")
    vhost_new_parser.add_argument("--alias", action="append")
    vhost_new_parser.add_argument("--port", type=valid_port)
    ssl_group = vhost_new_parser.add_mutually_exclusive_group()
    ssl_group.add_argument("--ssl", action="store_true", dest="ssl")
    ssl_group.add_argument("--no-ssl", action="store_false", dest="ssl")
    vhost_new_parser.set_defaults(ssl=None)
    vhost_new_parser.add_argument("--cert-name")
    map_host_group = vhost_new_parser.add_mutually_exclusive_group()
    map_host_group.add_argument("--map-host", action="store_true", dest="map_host")
    map_host_group.add_argument("--no-map-host", action="store_false", dest="map_host")
    vhost_new_parser.set_defaults(map_host=None)
    vhost_new_parser.add_argument("--replace", action="store_true")
    vhost_remove_parser = vhost_commands.add_parser("remove", help="remove a managed vhost")
    vhost_remove_parser.add_argument("name")
    vhost_commands.add_parser("test", help="run apachectl configtest")
    vhost_commands.add_parser("reload", help="reload Apache")

    host = commands.add_parser("host", help="manage local hosts file entries")
    host_commands = host.add_subparsers(dest="host_command", required=True)
    host_commands.add_parser("list", help="list hosts file entries")
    host_add_parser = host_commands.add_parser("add", help="add a managed hosts entry")
    host_add_parser.add_argument("name")
    host_add_parser.add_argument("--alias", action="append", default=[])
    host_add_parser.add_argument("--ip", default="127.0.0.1")
    host_add_parser.add_argument("--replace", action="store_true")
    host_remove_parser = host_commands.add_parser(
        "remove", help="remove a managed hosts entry"
    )
    host_remove_parser.add_argument("name")

    cert = commands.add_parser("cert", help="manage local certificates")
    cert_commands = cert.add_subparsers(dest="cert_command", required=True)
    cert_commands.add_parser("list", help="list certificates and keys")
    cert_install_parser = cert_commands.add_parser("install", help="import a certificate and key")
    cert_install_parser.add_argument("name")
    cert_install_parser.add_argument("--cert", required=True)
    cert_install_parser.add_argument("--key", required=True)
    cert_install_parser.add_argument("--force", action="store_true")
    cert_generate_parser = cert_commands.add_parser("generate", help="generate a self-signed certificate")
    cert_generate_parser.add_argument("name")
    cert_generate_parser.add_argument("--san", action="append", default=[])
    cert_generate_parser.add_argument("--days", type=int, default=825)
    cert_generate_parser.add_argument("--force", action="store_true")
    cert_remove_parser = cert_commands.add_parser("remove", help="remove a certificate and key")
    cert_remove_parser.add_argument("name")
    cert_remove_parser.add_argument("--yes", action="store_true")
    return parser


def dispatch(args: argparse.Namespace, config: dict[str, str]) -> None:
    if args.command in {None, "gui"}:
        launch_web_gui(args.config, config)
        return
    if args.command == "desktop":
        launch_gui(args.config, config)
        return

    if args.command == "config":
        if args.config_command == "show":
            config_show(args, config)
        else:
            config_set(args, config)
        return

    settings = resolve_settings(config)
    actions = {
        ("vhost", "list"): vhost_list,
        ("vhost", "show"): vhost_show,
        ("vhost", "add"): vhost_add,
        ("vhost", "new"): vhost_new,
        ("vhost", "remove"): vhost_remove,
        ("host", "list"): host_list,
        ("host", "add"): host_add,
        ("host", "remove"): host_remove,
        ("cert", "list"): cert_list,
        ("cert", "install"): cert_install,
        ("cert", "generate"): cert_generate,
        ("cert", "remove"): cert_remove,
    }
    subcommand = getattr(args, f"{args.command}_command")
    if (args.command, subcommand) in actions:
        actions[(args.command, subcommand)](args, settings)
    elif subcommand == "test":
        run_apachectl(settings, "configtest")
    elif subcommand == "reload":
        run_apachectl(settings, "-k", "graceful")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.config = args.config.expanduser().resolve()
    try:
        dispatch(args, read_config(args.config))
    except (VhostManagerError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nOperation cancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
