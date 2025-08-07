import json
import urllib.parse
from datetime import datetime, timezone

import click
import requests

from zucaro.cli.utils import pass_launcher
from zucaro.logging import logger
from zucaro.utils import Directory, die

_loader_name = "quilt"

PACKAGE = "org.quiltmc"
MAVEN_BASE = "https://maven.quiltmc.org/repository/release/"
LOADER_NAME = "quilt-loader"
MAPPINGS_NAME = "hashed"

__all__ = ["register_cli"]


class VersionError(Exception):
    pass


def latest_game_version():
    url = "https://meta.quiltmc.org/v3/versions/game"
    obj = requests.get(url).json()
    for ver in obj:
        if not ver.get("version", "").startswith("1."):
            continue
        return ver["version"]
    raise VersionError("Could not find a suitable game version")


def get_loader_meta(game_version, loader_version):
    # First get available loader versions
    url = "https://meta.quiltmc.org/v3/versions/loader"
    obj = requests.get(url).json()
    if len(obj) == 0:
        raise VersionError("No loader versions available")
    
    if loader_version and "+" in loader_version:
        loader_version = loader_version.split("+")[0]

    if loader_version is None:
        ver = obj[0]  # Latest version
    else:
        try:
            ver = next(v for v in obj if v["version"] == loader_version)
        except StopIteration:
            raise VersionError("Specified loader version is not available") from None

    # Get the launcher metadata for this specific combination
    launcher_meta_url = f"https://meta.quiltmc.org/v3/versions/loader/{game_version}/{ver['version']}/profile/json"
    launcher_meta = requests.get(launcher_meta_url).json()
    
    return ver["version"], launcher_meta


def resolve_version(game_version=None, loader_version=None):
    if game_version is None:
        game_version = latest_game_version()

    loader_version, loader_obj = get_loader_meta(game_version, loader_version)
    return game_version, loader_version, loader_obj


def generate_vspec_obj(version_name, loader_obj, loader_version, game_version):
    # For Quilt, we can use the profile JSON directly as it's already in the correct format
    vspec = loader_obj.copy()
    
    # Update the ID to our custom version name
    vspec["id"] = version_name
    
    # Ensure we have the correct jar reference to prevent duplication
    vspec["inheritsFrom"] = game_version
    vspec["jar"] = game_version
    
    # Add current timestamp
    vspec["time"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    
    # Ensure we have all required libraries
    if "libraries" not in vspec:
        vspec["libraries"] = []
    
    # Add Quilt loader library if not present
    loader_library = {
        "name": f"{PACKAGE}:{LOADER_NAME}:{loader_version}",
        "url": MAVEN_BASE
    }
    
    if loader_library not in vspec["libraries"]:
        vspec["libraries"].append(loader_library)
    
    return vspec


def install(versions_root, game_version=None, loader_version=None, version_name=None):
    game_version, loader_version, loader_obj = resolve_version(
        game_version, loader_version
    )

    if version_name is None:
        version_name = "{}-{}-{}".format(LOADER_NAME, loader_version, game_version)

    version_dir = versions_root / version_name
    if version_dir.exists():
        die(f"Version with name {version_name} already exists")

    msg = f"Installing Quilt version {loader_version}-{game_version}"
    if version_name:
        logger.info(msg + f" as {version_name}")
    else:
        logger.info(msg)

    vspec_obj = generate_vspec_obj(
        version_name, loader_obj, loader_version, game_version
    )

    # Ensure mainClass is set correctly
    if "mainClass" not in vspec_obj:
        vspec_obj["mainClass"] = "org.quiltmc.loader.impl.launch.knot.KnotClient"

    version_dir.mkdir()
    with open(version_dir / f"{version_name}.json", "w") as fd:
        json.dump(vspec_obj, fd, indent=2)


@click.group("quilt")
def quilt_cli():
    """The Quilt loader.

    Find out more about Quilt at https://quiltmc.org/"""
    pass


@quilt_cli.command("install")
@click.argument("game_version", required=False)
@click.argument("loader_version", required=False)
@click.option("--name", default=None)
@pass_launcher
def install_cli(launcher, game_version, loader_version, name):
    """Install Quilt. If no additional arguments are specified, the latest
    supported stable (non-snapshot) game version is chosen. The most recent
    loader version for the given game version is selected automatically. Both
    the game version and the loader version may be overridden."""
    versions_root = launcher.get_path(Directory.VERSIONS)
    try:
        install(
            versions_root,
            game_version,
            loader_version,
            version_name=name,
        )
    except VersionError as e:
        logger.error(e)
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to Quilt servers: {e}")


@quilt_cli.command("version")
@click.argument("game_version", required=False)
def version_cli(game_version):
    """Resolve the loader version. If game version is not specified, the latest
    supported stable (non-snapshot) is chosen automatically."""
    try:
        game_version, loader_version, _ = resolve_version(game_version)
        logger.info(f"{loader_version}-{game_version}")
    except VersionError as e:
        logger.error(e)
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to Quilt servers: {e}")


def register_cli(root):
    root.add_command(quilt_cli)