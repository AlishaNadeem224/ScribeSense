# Pinning the extension ID (O4 only)

The extension ID must be the same on every machine, because the native-messaging host manifest
allows exactly one ID (`allowed_origins`). Chromium derives the ID from the public `key` in
`manifest.json` (K11, decision 2026-10-12).

> **WARNING: never commit the private key.** Keep it outside this repository, readable only by
> you, and backed up somewhere safe. Anyone with the private key can publish an extension that the
> ScribeSense host will trust. Only the PUBLIC key goes into `manifest.json`.

## 1. Create the key pair (once, outside the repo)

Run these yourself. The folder `~/scribesense-keys` is only an example; any private folder that is
**not** inside the repository works.

```bash
mkdir -p ~/scribesense-keys
chmod 700 ~/scribesense-keys

# private key (2048-bit RSA, PKCS#8 PEM). Never copy this into the repo.
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 \
    -out ~/scribesense-keys/scribesense-extension.pem
chmod 600 ~/scribesense-keys/scribesense-extension.pem

# public key (SubjectPublicKeyInfo, "BEGIN PUBLIC KEY"). This is what set_key.py reads.
openssl pkey -in ~/scribesense-keys/scribesense-extension.pem -pubout \
    -out ~/scribesense-keys/scribesense-extension.pub.pem
```

## 2. Put the public key into manifest.json

```bash
python3 extension/tools/set_key.py ~/scribesense-keys/scribesense-extension.pub.pem
```

The tool writes the base64 DER public key into `manifest.json` as `"key"` and prints the extension
ID (32 letters `a`-`p`). It refuses any file that contains a private key.

To only print the ID without changing `manifest.json`:

```bash
python3 extension/tools/set_key.py --id-only ~/scribesense-keys/scribesense-extension.pub.pem
```

## 3. Check

1. Load `extension/` as an unpacked extension (Developer mode on the extensions page).
2. The ID shown there must equal the ID the tool printed.
3. The installer writes the host manifest with that ID
   (`scribesense.safety.native_host.render_host_manifest(extension_id, host_path)`).

Before committing, check that only `manifest.json` changed and that no `.pem` file is staged:

```bash
git status --short
```
