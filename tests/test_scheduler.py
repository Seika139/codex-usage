import unittest

from codex_usage.scheduler import JobSpec, _systemd_service


class SystemdServiceSerializationTests(unittest.TestCase):
    def test_working_directory_path_is_escaped_without_quotes(self) -> None:
        root = "/tmp/codex usage%root"
        spec = JobSpec(
            system="Linux",
            root=root,
            mise=f"{root}/bin/mise",
            db_path=f"{root}/state.db",
            environment_path="/opt/mise bin:/usr/bin",
            log_path="",
            owner_id="",
        )

        service = _systemd_service(spec).decode("utf-8")

        self.assertIn("WorkingDirectory=/tmp/codex usage%%root\n", service)
        self.assertIn('Environment="PATH=/opt/mise bin:/usr/bin"', service)
        self.assertIn(
            'Environment="CODEX_USAGE_DB=/tmp/codex usage%%root/state.db"',
            service,
        )
        self.assertIn(
            'ExecStart="/tmp/codex usage%%root/bin/mise" run collect', service
        )


if __name__ == "__main__":
    unittest.main()
