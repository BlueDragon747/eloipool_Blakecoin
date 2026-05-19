import py_compile
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


TEST_ROOT = Path(__file__).resolve().parents[1]
if (TEST_ROOT / "deploy-bundle").is_dir():
    BUNDLE = TEST_ROOT / "deploy-bundle"
elif TEST_ROOT.name == "eloipool" and (TEST_ROOT.parent / "config.py.template").is_file():
    BUNDLE = TEST_ROOT.parent
else:
    raise RuntimeError(f"Could not resolve deploy-bundle root from {TEST_ROOT}")


def py_string_literal(value: str) -> str:
    return repr(value)


def escape_sed_replacement(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    escaped = escaped.replace("&", r"\&")
    escaped = escaped.replace("|", r"\|")
    return escaped


def render_template(text: str, replacements: dict[str, str]) -> str:
    rendered = text
    for key, value in replacements.items():
        rendered = rendered.replace(key, value)
    return rendered


class TestMainnetBundleRelease(unittest.TestCase):
    CORE_BUNDLE_SYNC_FILES = (
        "eloipool.py",
        "merklemaker.py",
        "stratumserver.py",
        "networkserver.py",
        "merged-mine-proxy.py3",
        "config.py.example",
        "sharelogging/logfile.py",
        "sharelogging/sql.py",
    )

    def test_deploy_bundle_eloipool_core_files_match_source_tree(self):
        if not (TEST_ROOT / "deploy-bundle").is_dir():
            self.skipTest("bundle copy does not include source tree")

        for relpath in self.CORE_BUNDLE_SYNC_FILES:
            source_file = TEST_ROOT / relpath
            bundle_file = BUNDLE / "eloipool" / relpath
            self.assertTrue(source_file.is_file(), msg=f"missing source file {relpath}")
            self.assertTrue(bundle_file.is_file(), msg=f"missing bundled file {relpath}")
            self.assertEqual(
                source_file.read_bytes(),
                bundle_file.read_bytes(),
                msg=f"deploy-bundle/eloipool/{relpath} is stale; run deploy-bundle/scripts/build-bundle.sh",
            )

    def test_deploy_scripts_parse(self):
        for script in (
            BUNDLE / "deploy.sh",
            BUNDLE / "deploy-full-stack.sh",
            BUNDLE / "scripts" / "provision-daemons-remote.sh",
            BUNDLE / "scripts" / "build-runtime-daemon-images.sh",
        ):
            proc = subprocess.run(
                ["bash", "-n", str(script)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, msg=f"{script} failed bash -n: {proc.stderr}")

    def test_deploy_script_without_host_prints_usage(self):
        proc = subprocess.run(
            ["bash", str(BUNDLE / "deploy.sh")],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Usage: bash deploy.sh <host> [user] [password]", proc.stdout + proc.stderr)

    def test_config_template_renders_to_valid_python(self):
        template = (BUNDLE / "config.py.template").read_text()
        replacements = {
            "@@SERVER_NAME@@": escape_sed_replacement(py_string_literal("BlakeStream Eloipool 15.21")),
            "@@TRACKER_ADDR@@": escape_sed_replacement(py_string_literal("Babc123poolkeepwallet")),
            "@@COINBASER_CMD@@": escape_sed_replacement(py_string_literal("/opt/blakecoin-pool/bin/coinbaser.py %d %p")),
            "@@PARENT_RPC_URI@@": escape_sed_replacement(py_string_literal("http://user:pass@127.0.0.1:8772/")),
            "@@UPSTREAM_P2P_PORT@@": "8773",
            "@@POOL_SECRET_USER@@": escape_sed_replacement(py_string_literal("auxpow")),
            "@@GOTWORK_URI@@": escape_sed_replacement(py_string_literal("http://auxpow:auxpow@127.0.0.1:19335/")),
            "@@POOL_JSONRPC_PORT@@": "19334",
            "@@STRATUM_PORT@@": "3334",
            "@@SHARE_LOG_PATH@@": escape_sed_replacement(py_string_literal("/var/log/blakecoin-pool/share-logfile")),
            "@@ROTATING_LOG_PATH@@": escape_sed_replacement(py_string_literal("/var/log/blakecoin-pool/eloipool-rotating.log")),
        }
        rendered = render_template(template, replacements)
        rendered_body = "\n".join(rendered.splitlines()[2:])

        self.assertIsNone(re.search(r"@@[A-Z0-9_]+@@", rendered_body))
        self.assertIn("SecretUser = 'auxpow'", rendered)
        self.assertIn("GotWorkURI = 'http://auxpow:auxpow@127.0.0.1:19335/'", rendered)
        self.assertIn("BitcoinNodeAddresses = ()", rendered)
        self.assertIn("TrustedForwarders = ('::ffff:127.0.0.1', '127.0.0.1')", rendered)

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "config.py"
            out.write_text(rendered)
            py_compile.compile(str(out), doraise=True)

    def test_systemd_units_render_without_placeholders(self):
        replacements = {
            "@@INSTALL_ROOT@@": "/opt/blakecoin-pool",
            "@@LOG_ROOT@@": "/var/log/blakecoin-pool",
            "@@POOL_USER@@": "blakecoin",
            "@@POOL_GROUP@@": "blakecoin",
        }
        for unit in (
            BUNDLE / "systemd" / "blakecoin-pool.service",
            BUNDLE / "systemd" / "blakecoin-pool-proxy.service",
            BUNDLE / "systemd" / "blakecoin-pool-dashboard.service",
        ):
            rendered = render_template(unit.read_text(), replacements)
            self.assertNotIn("@@", rendered, msg=f"unresolved placeholder in {unit.name}")
            self.assertIn("/opt/blakecoin-pool", rendered)
            self.assertIn("/var/log/blakecoin-pool", rendered)

    def test_proxy_service_uses_proxy_env_and_venv_python(self):
        unit = (BUNDLE / "systemd" / "blakecoin-pool-proxy.service").read_text()
        self.assertIn("EnvironmentFile=-@@INSTALL_ROOT@@/config/proxy.env", unit)
        self.assertIn("merged-mine-proxy.py3 ${PROXY_ARGS}", unit)
        self.assertIn("@@INSTALL_ROOT@@/venv/bin/python", unit)

    def test_deploy_script_enforces_mining_readiness(self):
        script = (BUNDLE / "deploy.sh").read_text()
        self.assertIn('getblocktemplate \'{"rules":["segwit"]}\'', script)
        self.assertIn("createauxblock", script)
        self.assertIn("likely still syncing", script)

    def test_deploy_script_supports_daemon_install_modes(self):
        script = (BUNDLE / "deploy.sh").read_text()
        self.assertIn('DAEMON_INSTALL_MODE="${DAEMON_INSTALL_MODE:-existing}"', script)
        self.assertIn('DAEMON_IMAGE_TAG="${DAEMON_IMAGE_TAG:-15.21}"', script)
        self.assertIn('scripts/provision-daemons-remote.sh', script)

    def test_remote_provisioner_maps_source_and_container_inputs(self):
        script = (BUNDLE / "scripts" / "provision-daemons-remote.sh").read_text()
        self.assertIn("https://github.com/BlueDragon747/Blakecoin.git", script)
        self.assertIn("https://github.com/BlakeBitcoin/BlakeBitcoin.git", script)
        self.assertIn('IMAGE_TAG="${DAEMON_IMAGE_TAG:-15.21}"', script)
        self.assertIn("docker compose", script)

    def test_runtime_image_builder_targets_ubuntu24_outputs(self):
        script = (BUNDLE / "scripts" / "build-runtime-daemon-images.sh").read_text()
        self.assertIn("outputs/Ubuntu-24", script)
        self.assertIn('PRIMARY_TAG="15.21"', script)
        self.assertIn("docker build --pull", script)

    def test_full_deployer_supports_local_and_pull_modes_without_wipe_options(self):
        script = (BUNDLE / "deploy-full-stack.sh").read_text()
        self.assertIn('DAEMON_INSTALL_MODE="${DAEMON_INSTALL_MODE:-${MODE_FLAG:-local}}"', script)
        self.assertIn("https://github.com/BlueDragon747/Blakecoin.git", script)
        self.assertIn("https://github.com/BlakeBitcoin/BlakeBitcoin.git", script)
        self.assertIn('bash deploy-full-stack.sh --pull', script)
        self.assertIn('bash deploy-full-stack.sh [--bootstrap|--no-bootstrap]', script)
        self.assertIn('expected local or pull', script)
        self.assertIn('detected (%s)', script)
        self.assertIn('docker pull "${image}"', script)
        self.assertIn('MAINNET_SYNC_ROTATION="${MAINNET_SYNC_ROTATION:-true}"', script)
        self.assertIn('START_LOCAL_PEERS="${START_LOCAL_PEERS:-auto}"', script)
        self.assertIn('LOCAL_PEER_MIN_RAM_MB="${LOCAL_PEER_MIN_RAM_MB:-24576}"', script)
        self.assertIn('FINAL_DAEMON_SETTLE_S="${FINAL_DAEMON_SETTLE_S:-8}"', script)
        self.assertIn("Sequential mainnet daemon bootstrap/sync rotation", script)
        self.assertIn("wait_bootstrap_import_done", script)
        self.assertIn("mainnet daemon sync rotation complete", script)
        self.assertIn("Skipping local peer daemons", script)
        self.assertIn("START_LOCAL_PEERS must be auto, true, or false", script)
        self.assertIn("s/^bind=/#sync-disabled bind=/", script)
        self.assertIn("s/^#sync-disabled bind=/bind=/", script)
        self.assertIn('rpc_timeout = float(os.environ.get("RPC_TIMEOUT_S", "20"))', script)
        self.assertIn("RPC_TIMEOUT_S=60", script)
        self.assertIn("getwalletinfo", script)
        self.assertIn("Letting ${label} settle", script)
        self.assertIn("resuming incomplete download", script)
        self.assertIn("--tries=1", script)
        self.assertIn("failed with partial", script)
        self.assertIn("Background bootstrap downloads failed", script)
        self.assertIn("downloaded file is empty", script)
        self.assertIn("Final daemon memory health check", script)
        self.assertIn("FINAL_MIN_AVAILABLE_RAM_MB", script)
        self.assertIn("high swap use", script)
