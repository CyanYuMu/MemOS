"""Checks for the public container image contract."""

import unittest
from pathlib import Path


class ContainerContractTest(unittest.TestCase):
    def test_container_runs_as_non_root(self):
        text = (Path(__file__).resolve().parents[1] / "deploy/Dockerfile").read_text()
        self.assertIn("FROM scratch AS prod", text)
        self.assertIn("USER 65532:65532", text)
        self.assertIn('ENTRYPOINT ["/memory-console"]', text)


if __name__ == "__main__":
    unittest.main()
