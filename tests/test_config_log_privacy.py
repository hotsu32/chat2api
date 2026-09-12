"""Startup diagnostics must never print operator credentials."""
import os
import subprocess
import sys
from pathlib import Path


def test_startup_logs_omit_auth_and_proxy_credentials():
    env = dict(os.environ, PROXY_URL='socks5h://fixture-user:fixture-proxy-secret@proxy.example.test:1080',
               EXPORT_PROXY_URL='http://fixture-user:fixture-export-secret@proxy.example.test:8080',
               AUTHORIZATION='fixture-authorization-secret', AUTH_KEY='fixture-auth-key-secret')
    result = subprocess.run(
        [sys.executable, '-c',
         'from unittest.mock import patch\n'
         'with patch("dotenv.load_dotenv"):\n'
         '    import utils.configs\n'],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, 'configuration import failed (output withheld)'
    output = result.stdout + result.stderr
    leaked = any(secret in output for secret in (
        'fixture-proxy-secret', 'fixture-export-secret',
        'fixture-authorization-secret', 'fixture-auth-key-secret'))
    assert not leaked, 'startup diagnostics exposed fixture credentials (output withheld)'
