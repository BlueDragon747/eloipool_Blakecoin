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

## Deploying

```bash
cd /path/to/Blakestream-Eliopool-15.21
bash deploy-bundle/deploy.sh <host> [user] [password]
```

## Full Six-Chain Deploy

For a normal local deploy from a cloned repo on the VPS, use:

```bash
cd /path/to/Blakestream-Eliopool-15.21
bash deploy-bundle/deploy-full-stack.sh
```

To pull published daemon images instead of building coin daemons from source:

```bash
cd /path/to/Blakestream-Eliopool-15.21
bash deploy-bundle/deploy-full-stack.sh --pull
```

Remote deploy from a workstation is still available:

```bash
cd /path/to/Blakestream-Eliopool-15.21
bash deploy-bundle/deploy-full-stack.sh root@your-vps
```

Remote smoke test without changing the server:

```bash
cd /path/to/Blakestream-Eliopool-15.21
bash deploy-bundle/deploy-full-stack.sh root@your-vps --dry-run
```

`deploy-full-stack.sh` supports 2 daemon modes:

- default / `--local` builds the six coin daemons from source on the target
- `--pull` pulls the published `sidgrip/<coin>:15.21` daemon images on the target

By default, the script deploys `mainnet`.
Mainnet bootstrap downloads are enabled by default and run in the background
while daemon binaries are built or pulled. Use `--no-bootstrap` to rely only on
p2p sync.

After the bootstrap files are staged, the mainnet full-stack deploy performs a
solo sync rotation by default. It starts one primary daemon, imports
`bootstrap.dat`, catches up to the network tip, gracefully stops that daemon,
then moves to the next coin. After all six are synced and stopped, it starts
the final primary + local peer daemon pairs one coin at a time and then starts
the pool, proxy, and dashboard. Set `MAINNET_SYNC_ROTATION=false` only if you
intentionally want to skip that safety rotation.

Set one of these only when you want a different network:

- `NETWORK_MODE=testnet`
- `NETWORK_MODE=regtest`

Before deploying, it scans the target for existing BlakeStream systemd units,
daemon processes, and Docker containers and prints a summary of what it found.
That detection is informational only. The selected mode still controls the
deploy.
Existing managed services are stopped gracefully before redeploy. Coin datadirs
are preserved.
Use `--dry-run` to test SSH access, argument parsing, and existing-runtime
detection before a real install.

Important release settings:

- `deploy.sh` supports 3 daemon install modes:
  - `DAEMON_INSTALL_MODE=existing` keeps using daemons already on the host
  - `DAEMON_INSTALL_MODE=container` pulls `sidgrip/<coin>:15.21` images and runs them locally with Docker
  - `DAEMON_INSTALL_MODE=source` clones the upstream coin repos and compiles them on the host
- `deploy.sh` still discovers the 6 RPC conf files + CLI tools automatically after provisioning
- `DAEMON_INSTALL_MODE=container` is the preferred Docker deployment path
- source mode is wired to the upstream repo/branch map from the six coin build scripts:
  - Blakecoin `https://github.com/BlueDragon747/Blakecoin.git` `master`
  - BlakeBitcoin `https://github.com/BlakeBitcoin/BlakeBitcoin.git` `master`
  - Electron `https://github.com/BlueDragon747/Electron-ELT.git` `master`
  - Photon `https://github.com/BlueDragon747/photon.git` `master`
  - Lithium `https://github.com/BlueDragon747/lithium.git` `master`
  - UniversalMolecule `https://github.com/BlueDragon747/universalmol.git` `master`
- `MINING_KEY_SEGWIT_HRP` defaults to `blc`
- `TrackerAddr` is the pool keep / fallback wallet
- the child-chain pool payout addresses are generated automatically unless
  `POOL_AUX_ADDRESS_*` overrides are supplied
- `DASH_MINING_KEY_V2_COIN_HRPS` defaults to the full BlakeStream mainnet HRP set

Container-mode example:

```bash
cd /path/to/Blakestream-Eliopool-15.21
DAEMON_INSTALL_MODE=container \
DAEMON_IMAGE_NAMESPACE=sidgrip \
DAEMON_IMAGE_TAG=15.21 \
bash deploy-bundle/deploy.sh <host> [user] [password]
```

## Redeploy Behavior

The deploy script stops any existing BlakeStream services and containers that
match the managed stack, then redeploys the current bundle.

It does not wipe the host or remove the six coin datadirs.
