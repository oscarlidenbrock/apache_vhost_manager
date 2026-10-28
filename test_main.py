import contextlib
import importlib.util
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("vhost-manager.py")
MODULE_SPEC = importlib.util.spec_from_file_location("vhost_manager", MODULE_PATH)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError(f"Could not load {MODULE_PATH}")
vhost_manager = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = vhost_manager
MODULE_SPEC.loader.exec_module(vhost_manager)


class VhostManagerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.json"
        self.vhosts = self.root / "httpd-vhosts.conf"
        self.certs = self.root / "certs"
        self.apachectl = self.root / "apachectl"
        self.hosts = self.root / "hosts"
        self.vhosts.write_text("# Existing configuration\n", encoding="utf-8")
        self.hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")
        self.apachectl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.apachectl.chmod(0o755)
        self.invoke(
            "config",
            "set",
            "--vhosts-file",
            str(self.vhosts),
            "--certs-dir",
            str(self.certs),
            "--apachectl",
            str(self.apachectl),
            "--hosts-file",
            str(self.hosts),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = vhost_manager.main(["--config", str(self.config), *arguments])
        self.assertEqual(status, 0, output.getvalue())
        return output.getvalue()

    def test_add_list_show_and_remove_vhost(self):
        document_root = self.root / "public"
        self.invoke(
            "vhost",
            "add",
            "example.test",
            "--document-root",
            str(document_root),
            "--alias",
            "www.example.test",
        )

        content = self.vhosts.read_text(encoding="utf-8")
        self.assertIn("# BEGIN vhost-manager: example.test", content)
        self.assertIn("ServerAlias www.example.test", content)
        self.assertIn("example.test", self.invoke("vhost", "list"))
        self.assertIn("<VirtualHost *:80>", self.invoke("vhost", "show", "example.test"))
        self.assertEqual(len(list(self.root.glob("httpd-vhosts.conf.bak-*"))), 1)

        self.invoke("vhost", "remove", "example.test")
        self.assertNotIn("example.test", self.vhosts.read_text(encoding="utf-8"))

    def test_replace_document_root_with_unchanged_host_mapping(self):
        first_root = self.root / "first-public"
        second_root = self.root / "second-public"
        self.invoke(
            "vhost",
            "add",
            "replace.test",
            "--document-root",
            str(first_root),
            "--map-host",
        )

        self.invoke(
            "vhost",
            "add",
            "replace.test",
            "--document-root",
            str(second_root),
            "--map-host",
            "--replace",
        )

        content = self.vhosts.read_text(encoding="utf-8")
        self.assertIn(f'DocumentRoot "{second_root.resolve()}"', content)
        self.assertIn(f'<Directory "{second_root.resolve()}">', content)
        self.assertNotIn(str(first_root.resolve()), content)
        self.assertEqual(
            self.hosts.read_text(encoding="utf-8").count(
                "# BEGIN vhost-manager: replace.test"
            ),
            1,
        )

    def test_parse_vhosts_for_graphical_list(self):
        self.invoke(
            "vhost",
            "add",
            "parsed.test",
            "--document-root",
            str(self.root / "parsed"),
            "--alias",
            "www.parsed.test",
            "--port",
            "8080",
        )
        self.vhosts.write_text(
            self.vhosts.read_text(encoding="utf-8")
            + "\n<VirtualHost *:9000>\n"
            + "    ServerName manual.test\n"
            + f'    DocumentRoot "{self.root / "manual"}"\n'
            + "</VirtualHost>\n",
            encoding="utf-8",
        )

        records = vhost_manager.parse_vhosts(
            self.vhosts.read_text(encoding="utf-8")
        )

        self.assertEqual([record.name for record in records], ["parsed.test", "manual.test"])
        self.assertEqual(records[0].aliases, ("www.parsed.test",))
        self.assertEqual(records[0].port, 8080)
        self.assertTrue(records[0].managed)
        self.assertFalse(records[1].managed)

    def test_graphical_interface_is_the_default_command(self):
        with mock.patch.object(vhost_manager, "launch_web_gui") as launch_gui:
            status = vhost_manager.main(["--config", str(self.config)])

        self.assertEqual(status, 0)
        launch_gui.assert_called_once()

    def test_protected_file_uses_authorized_update(self):
        backup = self.root / "authorized-backup"
        with mock.patch.object(vhost_manager.os, "access", return_value=False), mock.patch.object(
            vhost_manager, "privileged_update_file", return_value=backup
        ) as privileged_update:
            result = vhost_manager.update_file(self.hosts, "old", "new")

        self.assertEqual(result, backup)
        privileged_update.assert_called_once_with(self.hosts, "old", "new")

    def test_install_certificate_and_create_ssl_vhost(self):
        source_cert = self.root / "source.crt"
        source_key = self.root / "source.key"
        source_cert.write_text("certificate", encoding="utf-8")
        source_key.write_text("private key", encoding="utf-8")
        self.invoke(
            "cert",
            "install",
            "secure.test",
            "--cert",
            str(source_cert),
            "--key",
            str(source_key),
        )
        self.invoke(
            "vhost",
            "add",
            "secure.test",
            "--document-root",
            str(self.root / "secure"),
            "--ssl",
        )

        content = self.vhosts.read_text(encoding="utf-8")
        self.assertIn("<VirtualHost *:443>", content)
        self.assertIn("SSLEngine on", content)
        self.assertEqual((self.certs / "secure.test.key").stat().st_mode & 0o777, 0o600)

    def test_does_not_replace_manual_vhost(self):
        self.vhosts.write_text(
            "<VirtualHost *:80>\nServerName manual.test\n</VirtualHost>\n",
            encoding="utf-8",
        )
        with contextlib.redirect_stderr(io.StringIO()):
            status = vhost_manager.main(
                [
                    "--config",
                    str(self.config),
                    "vhost",
                    "add",
                    "manual.test",
                    "--document-root",
                    str(self.root),
                    "--replace",
                ]
            )
        self.assertEqual(status, 1)
        self.assertNotIn("vhost-manager", self.vhosts.read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("openssl"), "openssl is not installed")
    def test_generate_self_signed_certificate(self):
        self.invoke(
            "cert",
            "generate",
            "generated.test",
            "--san",
            "www.generated.test",
            "--days",
            "30",
        )
        self.assertTrue((self.certs / "generated.test.crt").is_file())
        self.assertTrue((self.certs / "generated.test.key").is_file())

    @unittest.skipUnless(shutil.which("openssl"), "openssl is not installed")
    def test_ssl_vhost_generates_missing_certificate(self):
        output = self.invoke(
            "vhost",
            "add",
            "automatic.test",
            "--document-root",
            str(self.root / "automatic"),
            "--alias",
            "www.automatic.test",
            "--ssl",
        )

        self.assertIn("Self-signed certificate generated", output)
        self.assertTrue((self.certs / "automatic.test.crt").is_file())
        self.assertTrue((self.certs / "automatic.test.key").is_file())
        self.assertIn("SSLCertificateFile", self.vhosts.read_text(encoding="utf-8"))

    @mock.patch(
        "builtins.input",
        side_effect=[
            "interactive.test",
            "/tmp/interactive",
            "www.interactive.test",
            "no",
            "",
            "",
        ],
    )
    def test_new_vhost_prompts_for_missing_options(self, mocked_input):
        self.invoke("vhost", "new")

        content = self.vhosts.read_text(encoding="utf-8")
        self.assertIn("ServerName interactive.test", content)
        self.assertIn("ServerAlias www.interactive.test", content)
        self.assertIn("<VirtualHost *:80>", content)
        hosts = self.hosts.read_text(encoding="utf-8")
        self.assertIn("127.0.0.1\tinteractive.test www.interactive.test", hosts)
        self.assertEqual(mocked_input.call_count, 6)

    def test_add_list_and_remove_host_mapping(self):
        self.invoke(
            "host",
            "add",
            "app.test",
            "--alias",
            "www.app.test",
        )

        self.assertIn("app.test", self.invoke("host", "list"))
        settings = vhost_manager.resolve_settings(vhost_manager.read_config(self.config))
        self.assertTrue(vhost_manager.has_managed_host_mapping(settings, "app.test"))
        hosts = self.hosts.read_text(encoding="utf-8")
        self.assertIn("# BEGIN vhost-manager: app.test", hosts)
        self.assertIn("127.0.0.1\tapp.test www.app.test", hosts)

        self.invoke("host", "remove", "app.test")
        self.assertFalse(vhost_manager.has_managed_host_mapping(settings, "app.test"))
        self.assertNotIn("app.test", self.hosts.read_text(encoding="utf-8"))

    def test_manual_host_mapping_prevents_vhost_creation(self):
        self.hosts.write_text(
            "127.0.0.1 localhost\n127.0.0.1 manual-host.test\n",
            encoding="utf-8",
        )
        with contextlib.redirect_stderr(io.StringIO()):
            status = vhost_manager.main(
                [
                    "--config",
                    str(self.config),
                    "vhost",
                    "add",
                    "manual-host.test",
                    "--document-root",
                    str(self.root),
                    "--map-host",
                ]
            )

        self.assertEqual(status, 1)
        self.assertNotIn("manual-host.test", self.vhosts.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
