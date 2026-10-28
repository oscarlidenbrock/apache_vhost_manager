#!/usr/bin/env python3
"""Command line manager for Homebrew Apache virtual hosts and certificates."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence


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
    commands = parser.add_subparsers(dest="command", required=True)

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
