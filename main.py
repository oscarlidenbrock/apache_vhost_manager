#!/usr/bin/env python3
"""Command line manager for Homebrew Apache virtual hosts and certificates."""

from __future__ import annotations

import argparse
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
        "No se pudo detectar Homebrew. Configura las rutas con "
        "'config set --vhosts-file ... --certs-dir ... --apachectl ...'."
    )


def read_config(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VhostManagerError(f"No se pudo leer {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise VhostManagerError(f"La configuración de {path} debe ser un objeto JSON.")
    return {key: str(value) for key, value in data.items() if value is not None}


def resolve_settings(config: dict[str, str]) -> Settings:
    required = {"vhosts_file", "certs_dir", "apachectl"}
    if required.issubset(config):
        defaults: dict[str, Path] = {}
    else:
        prefix = brew_prefix()
        defaults = {
            "vhosts_file": prefix / "etc/httpd/extra/httpd-vhosts.conf",
            "certs_dir": prefix / "etc/httpd/certs",
            "apachectl": prefix / "bin/apachectl",
        }

    def path_for(key: str, env_name: str) -> Path:
        value = os.environ.get(env_name) or config.get(key) or defaults[key]
        return Path(value).expanduser().resolve()

    return Settings(
        vhosts_file=path_for("vhosts_file", "VHOST_MANAGER_VHOSTS_FILE"),
        certs_dir=path_for("certs_dir", "VHOST_MANAGER_CERTS_DIR"),
        apachectl=path_for("apachectl", "VHOST_MANAGER_APACHECTL"),
    )


def validate_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise VhostManagerError(
            f"Nombre no válido: {name!r}. Usa letras, números, puntos, guiones o '_'."
        )
    return name


def validate_directive_value(value: str, label: str) -> str:
    if "\n" in value or "\r" in value:
        raise VhostManagerError(f"{label} no puede contener saltos de línea.")
    return value


def valid_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("el puerto debe ser un número") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("el puerto debe estar entre 1 y 65535")
    return port


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
            f"No existe {settings.vhosts_file}. Corrige la ruta con 'config set'."
        ) from exc
    except OSError as exc:
        raise VhostManagerError(f"No se pudo leer {settings.vhosts_file}: {exc}") from exc


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
        raise VhostManagerError(f"No se pudo escribir {path}: {exc}") from exc


def update_vhosts_file(settings: Settings, old: str, new: str) -> Path:
    if old == new:
        raise VhostManagerError("No hay cambios que guardar.")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = settings.vhosts_file.with_name(
        f"{settings.vhosts_file.name}.bak-{timestamp}"
    )
    try:
        shutil.copy2(settings.vhosts_file, backup)
    except OSError as exc:
        raise VhostManagerError(f"No se pudo crear la copia {backup}: {exc}") from exc
    try:
        atomic_write(settings.vhosts_file, new)
    except Exception:
        shutil.copy2(backup, settings.vhosts_file)
        raise
    return backup


def managed_block_pattern(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?m)^\s*{re.escape(BEGIN_MARKER.format(name=name))}\s*$.*?"
        rf"^\s*{re.escape(END_MARKER.format(name=name))}\s*$\n?",
        re.DOTALL,
    )


def render_vhost(args: argparse.Namespace, settings: Settings) -> str:
    name = validate_name(args.name)
    validate_directive_value(args.document_root, "DocumentRoot")
    document_root = Path(args.document_root).expanduser().resolve()
    port = args.port if args.port is not None else (443 if args.ssl else 80)
    aliases = "".join(
        f"\n    ServerAlias {validate_name(alias)}" for alias in args.alias
    )
    ssl = ""
    if args.ssl:
        cert_name = validate_name(args.cert_name or name)
        cert = settings.certs_dir / f"{cert_name}.crt"
        key = settings.certs_dir / f"{cert_name}.key"
        if not cert.is_file() or not key.is_file():
            raise VhostManagerError(
                f"Faltan {cert} o {key}. Instala o genera el certificado primero."
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


def config_set(args: argparse.Namespace, config: dict[str, str]) -> None:
    changes = {
        "vhosts_file": args.vhosts_file,
        "certs_dir": args.certs_dir,
        "apachectl": args.apachectl,
    }
    if not any(changes.values()):
        raise VhostManagerError("Indica al menos una ruta para guardar.")
    for key, value in changes.items():
        if value:
            config[key] = str(Path(value).expanduser().resolve())
    args.config.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(args.config, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    print(f"Configuración guardada en {args.config}")


def vhost_list(_: argparse.Namespace, settings: Settings) -> None:
    content = read_vhosts_file(settings)
    matches = list(VHOST_RE.finditer(content))
    if not matches:
        print("No hay VirtualHost configurados.")
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
        raise VhostManagerError(f"No se encontró el vhost {name}.")
    print("\n\n".join(blocks))


def vhost_add(args: argparse.Namespace, settings: Settings) -> None:
    content = read_vhosts_file(settings)
    original_content = content
    block = render_vhost(args, settings)
    existing = [
        match
        for match in VHOST_RE.finditer(content)
        if directive(match.group("body"), "ServerName") == args.name
    ]
    managed = managed_block_pattern(args.name)
    if existing and not args.replace:
        raise VhostManagerError(
            f"Ya existe un vhost para {args.name}; usa --replace para sustituirlo."
        )
    if existing:
        match = managed.search(content)
        if not match:
            raise VhostManagerError(
                "El vhost existente no fue creado por esta herramienta y no se reemplazará."
            )
        content = content[: match.start()] + content[match.end() :]
    new_content = content.rstrip() + "\n\n" + block
    backup = update_vhosts_file(settings, original_content, new_content)
    print(f"Vhost {args.name} guardado. Copia de seguridad: {backup}")


def vhost_remove(args: argparse.Namespace, settings: Settings) -> None:
    name = validate_name(args.name)
    content = read_vhosts_file(settings)
    pattern = managed_block_pattern(name)
    match = pattern.search(content)
    if not match:
        raise VhostManagerError(
            f"No existe un vhost gestionado para {name}. Los bloques manuales no se borran."
        )
    new_content = (content[: match.start()] + content[match.end() :]).rstrip() + "\n"
    backup = update_vhosts_file(settings, content, new_content)
    print(f"Vhost {name} eliminado. Copia de seguridad: {backup}")


def run_apachectl(settings: Settings, *arguments: str) -> None:
    executable = settings.apachectl
    if not executable.is_file():
        raise VhostManagerError(f"No existe apachectl en {executable}.")
    result = subprocess.run(
        [str(executable), *arguments], capture_output=True, text=True, check=False
    )
    output = (result.stdout + result.stderr).strip()
    if output:
        print(output)
    if result.returncode:
        raise VhostManagerError(
            f"apachectl terminó con el código {result.returncode}."
        )


def cert_paths(settings: Settings, name: str) -> tuple[Path, Path]:
    validate_name(name)
    return settings.certs_dir / f"{name}.crt", settings.certs_dir / f"{name}.key"


def cert_list(_: argparse.Namespace, settings: Settings) -> None:
    if not settings.certs_dir.exists():
        print(f"No existe el directorio {settings.certs_dir}.")
        return
    names = sorted(
        {path.stem for path in settings.certs_dir.glob("*.crt")}
        | {path.stem for path in settings.certs_dir.glob("*.key")}
    )
    if not names:
        print("No hay certificados gestionados.")
        return
    print(f"{'NAME':35} {'CERT':5} KEY")
    for name in names:
        cert, key = cert_paths(settings, name)
        print(f"{name:35} {'yes' if cert.is_file() else 'no':5} {'yes' if key.is_file() else 'no'}")


def stage_private_file(source: Path, destination: Path, mode: int) -> Path:
    if not source.is_file():
        raise VhostManagerError(f"No existe el fichero de origen {source}.")
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
        raise VhostManagerError(f"No se pudo copiar {destination}: {exc}") from exc


def cert_install(args: argparse.Namespace, settings: Settings) -> None:
    cert, key = cert_paths(settings, args.name)
    source_cert = Path(args.cert).expanduser()
    source_key = Path(args.key).expanduser()
    for source in (source_cert, source_key):
        if not source.is_file():
            raise VhostManagerError(f"No existe el fichero de origen {source}.")
    existing = [path for path in (cert, key) if path.exists()]
    if existing and not args.force:
        raise VhostManagerError(
            f"Ya existe {existing[0]}; usa --force para reemplazar el par."
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
    print(f"Certificado instalado en {cert}; clave privada instalada en {key}")


def cert_generate(args: argparse.Namespace, settings: Settings) -> None:
    cert, key = cert_paths(settings, args.name)
    if args.days < 1:
        raise VhostManagerError("--days debe ser un número positivo.")
    if (cert.exists() or key.exists()) and not args.force:
        raise VhostManagerError("El certificado ya existe; usa --force para reemplazarlo.")
    openssl = shutil.which("openssl")
    if not openssl:
        raise VhostManagerError("No se encontró openssl en PATH.")
    settings.certs_dir.mkdir(parents=True, exist_ok=True)
    sans = list(dict.fromkeys(validate_name(name) for name in [args.name, *args.san]))
    with tempfile.TemporaryDirectory(dir=settings.certs_dir) as temporary:
        temp_cert = Path(temporary) / "certificate.crt"
        temp_key = Path(temporary) / "private.key"
        command = [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
            "-days", str(args.days), "-keyout", str(temp_key), "-out", str(temp_cert),
            "-subj", f"/CN={args.name}", "-addext",
            "subjectAltName=" + ",".join(f"DNS:{name}" for name in sans),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise VhostManagerError(result.stderr.strip() or "openssl falló.")
        os.replace(temp_cert, cert)
        os.replace(temp_key, key)
    cert.chmod(0o644)
    key.chmod(0o600)
    print(f"Certificado autofirmado generado en {cert}")


def cert_remove(args: argparse.Namespace, settings: Settings) -> None:
    cert, key = cert_paths(settings, args.name)
    existing = [path for path in (cert, key) if path.exists()]
    if not existing:
        raise VhostManagerError(f"No hay certificado para {args.name}.")
    if not args.yes:
        answer = input(f"¿Eliminar certificado y clave de {args.name}? [y/N] ").lower()
        if answer not in {"y", "yes", "s", "si", "sí"}:
            print("Operación cancelada.")
            return
    for path in existing:
        path.unlink()
    print(f"Certificado {args.name} eliminado.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Gestiona vhosts y certificados de Apache instalado con Homebrew.",
    )
    parser.add_argument(
        "--config", type=Path, default=default_config_path(),
        help="fichero JSON de configuración (por defecto: %(default)s)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    config = commands.add_parser("config", help="consulta o modifica las rutas")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("show", help="muestra la configuración efectiva")
    config_set_parser = config_commands.add_parser("set", help="guarda rutas alternativas")
    config_set_parser.add_argument("--vhosts-file")
    config_set_parser.add_argument("--certs-dir")
    config_set_parser.add_argument("--apachectl")

    vhost = commands.add_parser("vhost", help="gestiona VirtualHost de Apache")
    vhost_commands = vhost.add_subparsers(dest="vhost_command", required=True)
    vhost_commands.add_parser("list", help="lista los vhosts")
    vhost_show_parser = vhost_commands.add_parser("show", help="muestra un vhost")
    vhost_show_parser.add_argument("name")
    vhost_add_parser = vhost_commands.add_parser("add", help="crea un vhost")
    vhost_add_parser.add_argument("name", help="ServerName del vhost")
    vhost_add_parser.add_argument("--document-root", required=True)
    vhost_add_parser.add_argument("--alias", action="append", default=[])
    vhost_add_parser.add_argument("--port", type=valid_port)
    vhost_add_parser.add_argument("--ssl", action="store_true")
    vhost_add_parser.add_argument("--cert-name")
    vhost_add_parser.add_argument("--replace", action="store_true")
    vhost_remove_parser = vhost_commands.add_parser("remove", help="elimina un vhost gestionado")
    vhost_remove_parser.add_argument("name")
    vhost_commands.add_parser("test", help="ejecuta apachectl configtest")
    vhost_commands.add_parser("reload", help="recarga Apache")

    cert = commands.add_parser("cert", help="gestiona certificados locales")
    cert_commands = cert.add_subparsers(dest="cert_command", required=True)
    cert_commands.add_parser("list", help="lista certificados y claves")
    cert_install_parser = cert_commands.add_parser("install", help="importa certificado y clave")
    cert_install_parser.add_argument("name")
    cert_install_parser.add_argument("--cert", required=True)
    cert_install_parser.add_argument("--key", required=True)
    cert_install_parser.add_argument("--force", action="store_true")
    cert_generate_parser = cert_commands.add_parser("generate", help="genera un certificado autofirmado")
    cert_generate_parser.add_argument("name")
    cert_generate_parser.add_argument("--san", action="append", default=[])
    cert_generate_parser.add_argument("--days", type=int, default=825)
    cert_generate_parser.add_argument("--force", action="store_true")
    cert_remove_parser = cert_commands.add_parser("remove", help="elimina certificado y clave")
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
        ("vhost", "remove"): vhost_remove,
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
    except KeyboardInterrupt:
        print("\nOperación cancelada.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
