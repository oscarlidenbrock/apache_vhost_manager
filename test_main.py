import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

import main


class VhostManagerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.json"
        self.vhosts = self.root / "httpd-vhosts.conf"
        self.certs = self.root / "certs"
        self.apachectl = self.root / "apachectl"
        self.vhosts.write_text("# Existing configuration\n", encoding="utf-8")
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
        )

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = main.main(["--config", str(self.config), *arguments])
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
            status = main.main(
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


if __name__ == "__main__":
    unittest.main()
