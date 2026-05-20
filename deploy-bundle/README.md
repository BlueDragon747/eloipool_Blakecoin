# Blakestream-Eliopool-15.21 Mainnet Deploy Bundle

This bundle is the live deploy/runtime payload for the public mainnet release
repo `Blakestream-Eliopool-15.21`.

Active operator contract:

- bare `<40hex>[.worker]` is the only mining-key username form
- bare mining keys derive native-bech32 payouts through the configured HRP
- direct payout-address usernames still pass through as compatibility input

## Refreshing The Bundle

After changing the root repo, rebuild the vendored `deploy-bundle/eloipool/`
copy:

```bash
cd /path/to/Blakestream-Eliopool-15.21
bash deploy-bundle/scripts/build-bundle.sh
```

That keeps the shipped `deploy-bundle/eloipool/` tree aligned with the root
runtime/tests.

## Deployment Options

There are two deploy scripts. Most users should use the full-stack script.

| Script | Use It When | What It Deploys |
| --- | --- | --- |
| `deploy-full-stack.sh` | You want a fresh six-chain mainnet pool. | Coin daemons, configs, bootstraps, one-at-a-time sync, merged-mine proxy, Eloipool, dashboard, systemd services. |
| `deploy.sh` | You already have daemon RPCs running and only want to layer the pool on top. | Eloipool, merged-mine proxy, dashboard, and optional older daemon provisioning modes. |

## Full-Stack Deploy

`deploy-full-stack.sh` is the recommended installer. It defaults to mainnet,
preserves existing coin datadirs, and must run as root on local installs.

### Copy-Paste Local Installs

Fresh local VPS install, building all six daemons from source:

```bash
cd /path/to/Blakestream-Eliopool-15.21
sudo bash deploy-bundle/deploy-full-stack.sh
```

Fresh local VPS install, pulling published daemon binaries:

```bash
cd /path/to/Blakestream-Eliopool-15.21
sudo bash deploy-bundle/deploy-full-stack.sh --pull
```

Fresh local VPS install without bootstrap downloads:

```bash
cd /path/to/Blakestream-Eliopool-15.21
sudo bash deploy-bundle/deploy-full-stack.sh --no-bootstrap
```

Local VPS install with a public hostname and custom ports:

```bash
cd /path/to/Blakestream-Eliopool-15.21
PUBLIC_HOST=pool.example.com \
STRATUM_PORT=3334 \
DASHBOARD_PORT=18081 \
sudo bash deploy-bundle/deploy-full-stack.sh --pull
```

Local testnet install:

```bash
cd /path/to/Blakestream-Eliopool-15.21
NETWORK_MODE=testnet \
sudo bash deploy-bundle/deploy-full-stack.sh --pull --no-bootstrap
```

### Copy-Paste Remote Installs

Remote install from a workstation, building daemons on the VPS:

```bash
cd /path/to/Blakestream-Eliopool-15.21
bash deploy-bundle/deploy-full-stack.sh root@your-vps
```

Remote install from a workstation, pulling daemon binaries:

```bash
cd /path/to/Blakestream-Eliopool-15.21
bash deploy-bundle/deploy-full-stack.sh root@your-vps --pull
```

Remote install with custom public host and ports:

```bash
cd /path/to/Blakestream-Eliopool-15.21
PUBLIC_HOST=pool.example.com \
STRATUM_PORT=3334 \
DASHBOARD_PORT=18081 \
bash deploy-bundle/deploy-full-stack.sh root@your-vps --pull
```

Remote dry-run. This checks SSH, arguments, and existing-runtime detection
without changing the server:

```bash
cd /path/to/Blakestream-Eliopool-15.21
bash deploy-bundle/deploy-full-stack.sh root@your-vps --dry-run
```

Remote install with password auth if SSH keys are not set up:

```bash
cd /path/to/Blakestream-Eliopool-15.21
bash deploy-bundle/deploy-full-stack.sh your-vps root 'your-ssh-password'
```

### Full-Stack Flags

| Flag | Meaning |
| --- | --- |
| default / `--local` | Build all six coin daemons from source on the target. This is slower but does not depend on published images. |
| `--pull` | Pull `sidgrip/<coin>:15.21` images and extract daemon binaries. The final daemons still run as systemd services. |
| `--bootstrap` | Download mainnet bootstraps. This is the default. |
| `--no-bootstrap` | Skip bootstrap downloads and sync only from peers. |
| `--dry-run` | Check the target and show what would happen without installing. |
| `-h`, `--help` | Print usage. |

### Full-Stack Environment Options

| Variable | Default | Purpose |
| --- | --- | --- |
| `NETWORK_MODE` | `mainnet` | Set to `testnet` or `regtest` only when you do not want mainnet. |
| `INSTALL_ROOT` | `/opt/blakestream-mainnet` | Runtime install path. |
| `DATA_ROOT` | `/var/lib/blakestream-mainnet` | Coin datadirs and bootstrap staging. |
| `LOG_ROOT` | `/var/log/blakestream-mainnet` | Pool, proxy, daemon, and dashboard logs. |
| `PUBLIC_HOST` | target host / detected local IP | Hostname or IP shown on the dashboard and miner examples. |
| `STRATUM_PORT` | `3334` | Public stratum port. |
| `DASHBOARD_PORT` | `18081` | Public dashboard port. |
| `DAEMON_IMAGE_NAMESPACE` | `sidgrip` | Image namespace used with `--pull`. |
| `DAEMON_IMAGE_TAG` | `15.21` | Image tag used with `--pull`. |
| `BOOTSTRAP_BASE_URL` | `https://bootstrap.blakestream.io` | Base URL for mainnet `bootstrap.dat` downloads. |
| `MAINNET_SYNC_ROTATION` | `true` | Sync one daemon at a time before final service start. |
| `START_LOCAL_PEERS` | `auto` | Start extra local peer daemons when RAM is sufficient. Use `true` or `false` to force. |
| `USE_EXPLORER_PEERS` | `true` | Seed daemon configs with explorer-discovered peers on mainnet. |

Mainnet bootstrap downloads are enabled by default. They run one coin at a time
in a background task while daemon binaries are built or pulled. Existing
`bootstrap.dat`, consumed `bootstrap.dat.old`, and partial `.tmp` downloads are
preserved and reused on the next deploy attempt.

After bootstraps are staged, the mainnet deploy performs a safe sync rotation
by default. It starts one primary daemon, imports `bootstrap.dat`, catches up to
the network tip, gracefully stops that daemon, then moves to the next coin.
After all six are synced and stopped, it starts the final daemons one coin at a
time, then starts the pool, proxy, and dashboard.

During final startup, the installer temporarily gates the public stratum port
with a firewall rule. This prevents miners from submitting shares before the
merged-mining proxy has a usable aux template. The gate is removed only after
the pool JSON-RPC is live, the proxy port is bound, and the proxy answers
`getaux` with either a positive aux-chain readiness count or a usable aux blob.
If deploy fails before that readiness point, public stratum remains gated so
operators do not get confusing `gotwork` failures from a half-started pool.

The runtime has the same protection after startup through
`RequireGotworkReady = True`. Before accepting each stratum share, Eloipool
asks the proxy for a per-miner aux template. If the proxy has no usable aux
template, the share is rejected as `gotwork-unavailable` and the operator log
explains which proxy/aux readiness condition is missing. If only some aux
chains are lagging but at least one usable aux template exists, mining
continues with a partial-readiness warning.

Before deploying, the script scans the target for existing BlakeStream systemd
units, daemon processes, and Docker containers and prints a summary. Existing
managed services are stopped gracefully before redeploy. Coin datadirs are
preserved.

## Advanced Existing-Daemon Deploy

`deploy.sh` is the older/advanced deploy path for installing Eloipool against
daemon RPCs that already exist on a server. Use this only when you do not want
the full-stack installer to manage daemon setup and sync.

### Copy-Paste Existing-Daemon Installs

Deploy against daemons already running on the VPS:

```bash
cd /path/to/Blakestream-Eliopool-15.21
bash deploy-bundle/deploy.sh your-vps root
```

Deploy against existing daemons with a password:

```bash
cd /path/to/Blakestream-Eliopool-15.21
bash deploy-bundle/deploy.sh your-vps root 'your-ssh-password'
```

Ask `deploy.sh` to provision daemons from Docker containers:

```bash
cd /path/to/Blakestream-Eliopool-15.21
DAEMON_INSTALL_MODE=container \
DAEMON_IMAGE_NAMESPACE=sidgrip \
DAEMON_IMAGE_TAG=15.21 \
bash deploy-bundle/deploy.sh your-vps root
```

Ask `deploy.sh` to build daemons from source:

```bash
cd /path/to/Blakestream-Eliopool-15.21
DAEMON_INSTALL_MODE=source \
bash deploy-bundle/deploy.sh your-vps root
```

Deploy with explicit daemon RPC config paths:

```bash
cd /path/to/Blakestream-Eliopool-15.21
BLC_RPC_CONF=/root/.blakecoin/blakecoin.conf \
BBTC_RPC_CONF=/root/.blakebitcoin/blakebitcoin.conf \
ELT_RPC_CONF=/root/.electron/electron.conf \
LIT_RPC_CONF=/root/.lithium/lithium.conf \
PHO_RPC_CONF=/root/.photon/photon.conf \
UMO_RPC_CONF=/root/.universalmolecule/universalmolecule.conf \
bash deploy-bundle/deploy.sh your-vps root
```

### `deploy.sh` Options

`deploy.sh` accepts positional arguments only:

```bash
bash deploy-bundle/deploy.sh <host> [user] [password]
```

Useful `deploy.sh` environment options:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DAEMON_INSTALL_MODE` | `existing` | `existing`, `container`, or `source`. |
| `DAEMON_INSTALL_ROOT` | `/opt/blakestream-daemons` | Path used by `container` and `source` daemon provisioning. |
| `DAEMON_IMAGE_NAMESPACE` | `sidgrip` | Image namespace used by `DAEMON_INSTALL_MODE=container`. |
| `DAEMON_IMAGE_TAG` | `15.21` | Image tag used by `DAEMON_INSTALL_MODE=container`. |
| `INSTALL_ROOT` | `/opt/blakecoin-pool` | Eloipool/proxy/dashboard install path. |
| `LOG_ROOT` | `/var/log/blakecoin-pool` | Pool/proxy/dashboard logs. |
| `PUBLIC_HOST` | target host | Hostname or IP shown on the dashboard. |
| `STRATUM_PORT` | `3334` | Public stratum port. |
| `DASHBOARD_PORT` | `8080` | Public dashboard port for `deploy.sh`. |
| `MINING_KEY_SEGWIT_HRP` | `blc` | Parent-chain HRP for bare mining-key payout derivation. |
| `TRACKER_ADDR` | generated when unset | Pool keep / fallback Blakecoin wallet address. |
| `POOL_AUX_ADDRESS_*` | generated when unset | Optional fixed aux-chain pool payout addresses. |

`deploy.sh` source mode uses these upstream repos:

- Blakecoin `https://github.com/BlueDragon747/Blakecoin.git` `master`
- BlakeBitcoin `https://github.com/BlakeBitcoin/BlakeBitcoin.git` `master`
- Electron `https://github.com/BlueDragon747/Electron-ELT.git` `master`
- Photon `https://github.com/BlueDragon747/photon.git` `master`
- Lithium `https://github.com/BlueDragon747/lithium.git` `master`
- UniversalMolecule `https://github.com/BlueDragon747/universalmol.git` `master`

## Redeploy Behavior

The deploy script stops any existing BlakeStream services and containers that
match the managed stack, then redeploys the current bundle.

It does not wipe the host or remove the six coin datadirs.
