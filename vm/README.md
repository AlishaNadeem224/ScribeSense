# Full-system test VM (M3.5)

**These tests must never run on the development machine.** They change real settings (Flatpak overrides, Hyprland
keybinds, fontconfig, login hooks) and reboot the session. `vm/guard.py` refuses to run unless the machine carries
the provisioning marker **and** the run flag — a VM check alone is not enough, because the development machine can
itself be a VM (this one is QEMU).

## Status

| Part | State |
|---|---|
| Guard (`vm/guard.py`) | ✅ unit-tested without a VM (`tests/unit/test_vm_guard.py`) |
| Early check "does Hyprland run here?" (`python -m vm.check_hyprland`) | ✅ written and tested for its refusal and its report logic; **not yet run in a real VM** |
| VM image, snapshot-reset script | ⬜ needs the hypervisor decision below |
| Scenarios (apply → kill → reboot → revert, keybind → reset, Flatpak, uninstall) | ⬜ need `scribesense` CLI wiring (O2: `apply`, `reset`, `recover --login`, `confirm`) and M3.8 |

## Open decision (O3 + team)

Which hypervisor and how the snapshot is reset (libvirt/QEMU `virsh snapshot-revert`, a `qemu-img` overlay thrown
away after each run, UTM, …). Hyprland needs working GPU acceleration, so the choice must be proven with
`vm.check_hyprland` **first**. Until then no reset script is written — a script for the wrong hypervisor would only
look like progress.

## Provisioning a fresh VM (by hand, once, inside the VM)

1. Install Fedora + Hyprland and the dependencies: `sudo dnf install hyprland flatpak bubblewrap fontconfig pango gsettings-desktop-schemas gobject-introspection qt6ct lua`
2. Check out the repo, `uv venv --python /usr/bin/python3 --system-site-packages && uv sync`.
3. Run `uv run scribesense doctor` — every required item must be OK.
4. Create the marker (this is what tells the guard it is allowed to run here):
   `printf 'scribesense disposable test vm — reset from snapshot after every run\n' | sudo tee /etc/scribesense-test-vm`
5. Log in to a real Hyprland session, then `SCRIBESENSE_FULL_SYSTEM_TESTS=1 uv run python -m vm.check_hyprland`.
6. Take the snapshot. **Every run starts from it and ends by discarding the run's changes.**

Never create the marker on a machine you care about.
