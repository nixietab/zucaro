import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path

import click
import requests

from zucaro.cli.utils import pass_instance_manager, pass_launcher
from zucaro.downloader import DownloadQueue
from zucaro.instance import sanitize_name
from zucaro.logging import logger


def resolve_pack_meta(pack_id, version=None):
    """Resolve modpack metadata from Modrinth API."""
    base_url = "https://api.modrinth.com/v2"
    pack_manifest_url = f"{base_url}/project/{pack_id}"
    pack_versions_url = f"{base_url}/project/{pack_id}/version"

    try:
        pack_manifest = requests.get(pack_manifest_url).json()
        pack_versions = requests.get(pack_versions_url).json()
    except requests.RequestException as e:
        raise ValueError(f"Failed to fetch modpack data: {e}")

    if version:
        version_manifest = next((v for v in pack_versions if v["version_number"] == version), None)
        if not version_manifest:
            raise ValueError(f"Version {version} not found for pack {pack_id}")
    else:
        version_manifest = max(pack_versions, key=lambda v: v["date_published"])

    return pack_manifest, version_manifest


def verify_file_hash(file_path: Path, hashes: dict) -> bool:
    """Verify file hash matches expected hash."""
    if not file_path.exists():
        return False

    with open(file_path, 'rb') as f:
        content = f.read()

    if 'sha512' in hashes:
        calculated = hashlib.sha512(content).hexdigest()
        return calculated == hashes['sha512']
    elif 'sha1' in hashes:
        calculated = hashlib.sha1(content).hexdigest()
        return calculated == hashes['sha1']

    return False


def clean_conflicting_libraries(minecraft_dir: Path):
    """Remove older versions of libraries when conflicts are detected."""
    libraries_dir = minecraft_dir.parent / "libraries"
    if not libraries_dir.exists():
        return

    # Map of library base names to their versions and full paths
    library_versions = {}

    # Scan libraries directory
    for root, _, files in os.walk(libraries_dir):
        for file in files:
            if file.endswith('.jar'):
                path = Path(root) / file
                # Extract library name and version
                parts = path.stem.split('-')
                if len(parts) >= 2:
                    base_name = '-'.join(parts[:-1])
                    version = parts[-1]

                    if base_name not in library_versions:
                        library_versions[base_name] = []
                    library_versions[base_name].append((version, path))

    # Check for and resolve conflicts
    for lib_name, versions in library_versions.items():
        if len(versions) > 1:
            # Sort by version number (assuming semantic versioning)
            versions.sort(key=lambda x: [int(p) if p.isdigit() else p
                                      for p in x[0].split('.')])

            # Keep the newest version, remove others
            for version, path in versions[:-1]:
                logger.info(f"Removing older version of {lib_name}: {version}")
                try:
                    path.unlink()
                except Exception as e:
                    logger.warning(f"Failed to remove {path}: {e}")


def process_mrpack(mrpack_path: Path, target_dir: Path, download_queue: DownloadQueue):
    """Process a .mrpack file and queue files for download."""
    with zipfile.ZipFile(mrpack_path, 'r') as zip_ref:
        # Read the modrinth.index.json
        try:
            with zip_ref.open('modrinth.index.json') as index_file:
                index_data = json.load(index_file)
        except KeyError:
            raise ValueError("Invalid .mrpack file: missing modrinth.index.json")
        except json.JSONDecodeError:
            raise ValueError("Invalid modrinth.index.json format")

        logger.debug(f"Modpack format version: {index_data.get('format_version', 'unknown')}")

        # Create minecraft directory if it doesn't exist
        target_dir.mkdir(parents=True, exist_ok=True)

        # Extract overrides first
        for file_name in zip_ref.namelist():
            if file_name.startswith('overrides/'):
                try:
                    # Remove 'overrides/' prefix when extracting
                    relative_path = file_name[10:]
                    if relative_path:
                        target_path = target_dir / relative_path
                        target_path.parent.mkdir(parents=True, exist_ok=True)

                        with zip_ref.open(file_name) as source, open(target_path, 'wb') as target:
                            target.write(source.read())
                        logger.debug(f"Extracted override: {file_name} -> {target_path}")
                except Exception as e:
                    logger.warning(f"Failed to extract override {file_name}: {e}")

        # Process files from the index
        for file_entry in index_data.get('files', []):
            path = file_entry.get('path')
            if not path:
                logger.warning(f"Skipping entry with no path: {file_entry}")
                continue

            target_path = target_dir / path
            target_path.parent.mkdir(parents=True, exist_ok=True)

            if 'downloads' in file_entry and file_entry['downloads']:
                # File needs to be downloaded
                if not (target_path.exists() and verify_file_hash(target_path, file_entry.get('hashes', {}))):
                    download_queue.add(file_entry['downloads'][0], target_path)
                    logger.debug(f"Queued download: {path}")
            elif path in zip_ref.namelist():
                # File is included in the mrpack
                try:
                    with zip_ref.open(path) as source, open(target_path, 'wb') as target:
                        target.write(source.read())
                    logger.debug(f"Extracted file: {path}")
                except Exception as e:
                    logger.warning(f"Failed to extract file {path}: {e}")

        # Get Minecraft version from dependencies
        dependencies = index_data.get('dependencies', {})
        minecraft_version = dependencies.get('minecraft', '')

        return minecraft_version


def install(pack_id, version, launcher, im, instance_name):
    """Install a Modrinth modpack."""
    try:
        pack_manifest, version_manifest = resolve_pack_meta(pack_id, version)
    except ValueError as ex:
        logger.error(ex)
        return

    pack_name = pack_manifest["title"]
    pack_version = version_manifest["version_number"]

    if instance_name is None:
        instance_name = sanitize_name(f"{pack_name}-{pack_version}")

    if im.exists(instance_name):
        logger.error("Instance {} already exists".format(instance_name))
        return

    logger.info(f"Installing {pack_name} {pack_version} as {instance_name}")

    # Find the primary .mrpack file
    mrpack_file = next((f for f in version_manifest["files"] if f.get("primary", False)), None)

    if not mrpack_file:
        logger.error("No primary .mrpack file found in modpack")
        return

    # Download the .mrpack file
    with tempfile.NamedTemporaryFile(suffix='.mrpack', delete=False) as tmp_file:
        logger.info("Downloading modpack file...")
        response = requests.get(mrpack_file["url"], stream=True)
        for chunk in response.iter_content(chunk_size=8192):
            tmp_file.write(chunk)
        tmp_file.flush()

        mrpack_path = Path(tmp_file.name)

    try:
        # Create minecraft instance
        inst = im.create(instance_name, version_manifest.get('game_versions', [''])[0])
        inst.config["java.memory.max"] = "4096M"  # Default to 4GB RAM

        mcdir: Path = inst.get_minecraft_dir()
        dq = DownloadQueue()

        # Process the .mrpack file
        logger.info("Processing modpack contents...")
        minecraft_version = process_mrpack(mrpack_path, mcdir, dq)

        # Update instance Minecraft version if we got it from the mrpack
        if minecraft_version:
            inst.minecraft_version = minecraft_version

        logger.info("Downloading modpack files...")
        dq.download()

        logger.info("Checking for library conflicts...")
        clean_conflicting_libraries(mcdir)

        logger.info(f"Installed successfully as {instance_name}")

    except Exception as e:
        logger.error(f"Failed to install modpack: {e}")
        im.delete(instance_name)
        raise

    finally:
        # Clean up temporary .mrpack file
        try:
            mrpack_path.unlink()
        except Exception:
            pass


@click.group("modrinth")
def modrinth_cli():
    """Handles Modrinth modpacks"""
    pass


@modrinth_cli.command("install")
@click.argument("pack_id")
@click.argument("version", required=False)
@click.option("--name", "-n", default=None, help="Name of the resulting instance")
@pass_instance_manager
@pass_launcher
def install_cli(launcher, im, pack_id, name, version):
    """Install a Modrinth modpack.

    An instance is created with the correct version of all mods from the pack installed.

    PACK_ID is the slug from the URL to the pack's page on Modrinth.

    VERSION is the version number, for example 1.0.0. If VERSION is not
    specified, the latest is automatically chosen."""
    install(pack_id, version, launcher, im, name)


@modrinth_cli.command("fix-libraries")
@click.argument("instance_name")
@pass_instance_manager
def fix_libraries_cli(im, instance_name):
    """Fix library conflicts in an existing instance."""
    if not im.exists(instance_name):
        logger.error(f"Instance {instance_name} does not exist")
        return

    inst = im.get(instance_name)
    mcdir = inst.get_minecraft_dir()

    logger.info(f"Fixing library conflicts for instance {instance_name}...")
    clean_conflicting_libraries(mcdir)
    logger.info("Library conflicts resolved")


def register_cli(root):
    """Register Modrinth commands with the main CLI."""
    root.add_command(modrinth_cli)
