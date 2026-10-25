#!/usr/bin/env python3
"""Run upstream FairPlay retrieval locally on the owner's jailbroken iPhone."""
import argparse
import hashlib
import struct
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "upstream" / "credential-retrieval"))
import retrieve as upstream

class LocalDevice:
    command = [shutil.which("sh") or "/var/jb/bin/sh", "-c"]

    def __init__(self):
        self.directory = tempfile.mkdtemp(prefix="fp-retrieve-", dir="/var/tmp")
        self.trigger_directory = tempfile.mkdtemp(prefix="fp-trigger-", dir="/var/tmp")
        os.chmod(self.directory, 0o700)
        os.chmod(self.trigger_directory, 0o755)

    def run(self, command, data=None):
        result = subprocess.run(self.command + [command], input=data,
                                capture_output=True, timeout=120)
        if result.returncode:
            raise RuntimeError("device command failed (%d): %s" %
                               (result.returncode, (result.stdout + result.stderr).decode("utf-8", "replace")))
        return result.stdout

    def processes(self):
        return self.run("ps -e -o pid,comm")

    def path(self, name):
        if Path(name).name != name:
            raise ValueError("invalid filename")
        return str(Path(self.directory) / name)

    def write(self, name, source, executable=False):
        path = self.path(name)
        data = source.read_bytes() if isinstance(source, Path) else source
        upstream.write_new(Path(path), data)
        if executable:
            os.chmod(path, 0o700)
        return path

    def read(self, path):
        return Path(path).read_bytes()

    def cleanup(self):
        shutil.rmtree(self.directory)
        shutil.rmtree(self.trigger_directory)

def apps():
    result = []
    for app in Path("/var/containers/Bundle/Application").glob("*/*.app"):
        try:
            info = plistlib.loads((app / "Info.plist").read_bytes())
            binary = info["CFBundleExecutable"]
            if not isinstance(binary, str) or Path(binary).name != binary:
                continue
            for license_path in sorted((app / "SC_Info").glob("*.sinf")):
                sinf = license_path.read_bytes()
                if upstream.supported_sinf(sinf):
                    result.append((info.get("CFBundleDisplayName", app.stem),
                                   info.get("CFBundleIdentifier", ""), app, binary, sinf))
                    break
        except (OSError, KeyError, ValueError, plistlib.InvalidFileException):
            continue
    return sorted(result, key=lambda row: (row[0], row[1]))


def trust_binaries(bundle):
    jbctl = shutil.which("jbctl")
    if not jbctl and Path("/var/jb/basebin/jbctl").is_file():
        jbctl = "/var/jb/basebin/jbctl"
    if not jbctl:
        raise RuntimeError("Dopamine jbctl was not found. Ensure jailbreak is active.")
    for path in sorted(bundle.glob("*.ios")):
        data = path.read_bytes()
        if data[:4] != b"\xcf\xfa\xed\xfe":
            raise RuntimeError("Expected a thin arm64 Mach-O: " + path.name)
        pos = 32
        signature = None
        for _ in range(struct.unpack_from("<I", data, 16)[0]):
            cmd, size = struct.unpack_from("<II", data, pos)
            if size < 8 or pos + size > len(data):
                raise RuntimeError("Invalid load command")
            if cmd == 0x1d:
                offset, length = struct.unpack_from("<II", data, pos + 8)
                signature = data[offset:offset + length]
            pos += size
        if signature is None or struct.unpack_from(">I", signature)[0] != 0xfade0cc0:
            raise RuntimeError("Missing code signature: " + path.name)
        found = False
        for i in range(struct.unpack_from(">I", signature, 8)[0]):
            slot, offset = struct.unpack_from(">II", signature, 12 + 8 * i)
            magic, length = struct.unpack_from(">II", signature, offset)
            if magic != 0xfade0c02:
                continue
            directory = signature[offset:offset + length]
            hash_type = directory[37]
            if hash_type == 1:
                digest = hashlib.sha1(directory).digest()[:20]
            elif hash_type in (2, 3):
                digest = hashlib.sha256(directory).digest()[:20]
            else:
                continue
            result = subprocess.run([jbctl, "trustcache", "add", digest.hex()],
                                    capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise RuntimeError("Dopamine could not trust " + path.name + ": " + result.stderr + result.stdout)
            found = True
        if not found:
            raise RuntimeError("No supported CodeDirectory: " + path.name)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", help="installed App Store app to select")
    parser.add_argument("--output-dir", default="/var/mobile/Documents/FairPlay-output",
                        type=Path, help="NEW output directory; never overwrite")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root in NewTerm: su -")
    os.umask(0o077)
    if args.output_dir.exists():
        parser.error("output directory exists; choose another with --output-dir")
    choices = apps()
    if args.bundle_id:
        choices = [row for row in choices if row[1] == args.bundle_id]
    if not choices:
        parser.error("No installed App Store app has a supported license.")
    if len(choices) > 1:
        for n, row in enumerate(choices, 1):
            print("%d. %s [%s]" % (n, row[0], row[1]))
        try:
            index = int(input("Select the app/account for your encrypted IPA: ")) - 1
        except (ValueError, EOFError):
            parser.error("Enter a listed number.")
        if not 0 <= index < len(choices):
            parser.error("Number is outside the list.")
        selected = choices[index]
    else:
        selected = choices[0]
    label, bundle_id, app, binary, sinf = selected
    print("Selected: %s [%s]" % (label, bundle_id))
    bundle = HERE / "bundle"
    for name in upstream.DEVICE_FILES:
        if not (bundle / name).is_file():
            parser.error("Missing bundle file: " + name)
    trust_binaries(bundle)
    device = LocalDevice()
    try:
        for name in upstream.DEVICE_FILES:
            device.write(name, bundle / name, executable=name.endswith(".ios"))
        license_path = device.write("license.sinf", sinf)
        copied_app = Path(device.trigger_directory) / app.name
        shutil.copytree(app, copied_app, symlinks=True)
        uid_path = device.path("uid-layer.bin")
        print("Starting upstream observation (up to 100 seconds)...", flush=True)
        upstream.observe_uid_layer(
            device, device.path("credential.ios"), upstream.restart_fairplayd(device),
            uid_path, device.path("trigger.ios"), str(copied_app / binary))
        output = device.path("credential.bin")
        import shlex
        device.run(" ".join(map(shlex.quote, (
            device.path("credential.ios"), "retrieve", license_path, uid_path, output))))
        credential = device.read(output)
        if len(credential) != 16:
            raise RuntimeError("Credential must be exactly 16 bytes.")
        args.output_dir.mkdir(parents=True, mode=0o700)
        upstream.write_new(args.output_dir / "credential.bin", credential)
        # Permit the device owner to share the result through Filza, without making it public.
        for path in (args.output_dir, args.output_dir / "credential.bin"):
            os.chown(path, 501, 501)
        print("Saved: " + str(args.output_dir / "credential.bin"))
        print("Credential content was not printed. Keep it private.")
    finally:
        device.cleanup()

if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print("FAILED: " + str(error), file=sys.stderr)
        sys.exit(1)
