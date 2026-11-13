import hashlib
import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional

import requests

from zucaro.downloader import DownloadQueue
from zucaro.errors import PicomcError
from zucaro.logging import logger
from zucaro.osinfo import osinfo
from zucaro.utils import Directory, file_sha1, sanitize_name

EMPTY_ASSET_INDEX_ID = "mmc-empty"
EMPTY_ASSET_INDEX = b'{"objects": {}}'
EMPTY_ASSET_INDEX_SHA1 = hashlib.sha1(EMPTY_ASSET_INDEX).hexdigest()

ASSET_PLACEHOLDERS = (
    "${assets_root}",
    "${assets_index_name}",
    "${game_assets}",
)

# Keys that are Prism/MultiMC bookkeeping and have no place in a zucaro.
IGNORED_PATCH_KEYS = {
    "+traits",
    "MMC-hint",
    "compatibleJavaMajors",
    "compatibleJavaName",
    "formatVersion",
    "id",
    "inheritsFrom",
    "jarMods",
    "mainJar",
    "name",
    "requires",
    "uid",
    "version",
}

SIGNATURE_SUFFIXES = (".sf", ".dsa", ".rsa", ".ec")

NATIVE_SUFFIXES = (
    "-natives-linux",
    "-natives-linux-arm64",
    "-natives-linux-arm32",
    "-natives-windows",
    "-natives-windows-x86",
    "-natives-windows-arm64",
    "-natives-macos",
    "-natives-macos-arm64",
)


class MMCError(PicomcError):
    """Raised when a MultiMC/Prism export cannot be imported."""


class VersionExistsError(MMCError):
    def __init__(self, name):
        super().__init__(
            "Version '{}' already exists. Use --version-name to choose "
            "another name.".format(name)
        )
        self.name = name


class InstanceExistsError(MMCError):
    def __init__(self, name):
        super().__init__(
            "Instance '{}' already exists. Use --name to choose another "
            "name.".format(name)
        )
        self.name = name


@dataclass
class MMCComponent:
    uid: str
    version: str
    patch: dict
    raw: dict


@dataclass
class MMCPack:
    path: Path
    pathmap: Dict[str, str]
    pack: dict
    instance_cfg: dict
    components: List[MMCComponent]

    @classmethod
    def load(cls, path):
        path = Path(path)
        try:
            archive = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as e:
            raise MMCError("Could not open {}: {}".format(path, e))

        with archive as zf:
            pathmap_member = _find_member(zf, {}, "pathmap.json")
            pathmap = _read_json(zf, pathmap_member) if pathmap_member else {}

            pack_member = _find_member(zf, pathmap, "mmc-pack.json")
            pack = _read_json(zf, pack_member) if pack_member else None
            if pack is None:
                raise MMCError(
                    "This zip does not contain mmc-pack.json. It does not "
                    "look like a MultiMC/Prism instance export."
                )

            cfg_member = _find_member(zf, pathmap, "instance.cfg")
            cfg_text = _read_text(zf, cfg_member) if cfg_member else ""

            components = []
            for raw in pack.get("components", []):
                uid = raw.get("uid")
                if not uid:
                    continue
                patch_member = _find_member(zf, pathmap, "patches/{}.json".format(uid))
                patch = _read_json(zf, patch_member) if patch_member else {}
                patch = patch or {}
                version = (
                    raw.get("version")
                    or patch.get("version")
                    or raw.get("cachedVersion")
                    or ""
                )
                components.append(
                    MMCComponent(
                        uid=uid,
                        version=str(version),
                        patch=patch,
                        raw=raw,
                    )
                )

        return cls(
            path=path,
            pathmap=pathmap,
            pack=pack,
            instance_cfg=parse_instance_cfg(cfg_text),
            components=components,
        )

    def to_dict(self):
        """Return all metadata parsed from the export."""
        version_name = _default_version_name(self.components)
        instance_name = _default_instance_name(self, version_name)
        return {
            "path": str(self.path),
            "name": self.instance_cfg.get("name") or instance_name,
            "version_name": version_name,
            "instance_name": instance_name,
            "instance_cfg": dict(self.instance_cfg),
            "pack": self.pack,
            "pathmap": dict(self.pathmap),
            "components": [
                {
                    "uid": component.uid,
                    "version": component.version,
                    "patch": component.patch,
                    "raw": component.raw,
                }
                for component in self.components
            ],
        }

    def read_files(self, include_data=True):
        files = {}
        try:
            archive = zipfile.ZipFile(self.path)
        except (OSError, zipfile.BadZipFile) as e:
            raise MMCError("Could not open {}: {}".format(self.path, e))

        with archive as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                entry = {
                    "size": info.file_size,
                    "compressed_size": info.compress_size,
                    "crc": info.CRC,
                }
                if include_data:
                    entry["data"] = zf.read(info)
                files[info.filename] = entry
        return files

    def read_all(self, include_files=True, include_data=True):
        data = self.to_dict()
        if include_files:
            data["files"] = self.read_files(include_data=include_data)
        return data


def read_instance(path, include_files=True, include_data=True):
    return MMCPack.load(path).read_all(
        include_files=include_files,
        include_data=include_data,
    )


@dataclass
class ImportResult:
    version_name: str
    version_dir: Path
    instance_name: Optional[str] = None


def _read_json(zf, member):
    try:
        with zf.open(member) as fd:
            return json.load(fd)
    except KeyError:
        return None
    except json.JSONDecodeError as e:
        raise MMCError("Invalid JSON in {}: {}".format(member, e))


def _read_text(zf, member):
    try:
        with zf.open(member) as fd:
            return fd.read().decode("utf-8", "replace")
    except KeyError:
        return ""


def _find_member(zf, pathmap, logical_name):
    logical = "/" + logical_name.lstrip("/")
    bare_logical = logical.lstrip("/")
    for member, dest in pathmap.items():
        if dest.lstrip("/") == bare_logical:
            return member

    direct = logical.lstrip("/")
    names = zf.namelist()
    if direct in names:
        return direct

    matches = [name for name in names if name.endswith(direct)]
    if len(matches) == 1:
        return matches[0]
    return None


def parse_instance_cfg(text):
    cfg = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        cfg[key.strip()] = value.strip()
    return cfg


def _get_base_vspec(launcher, version_id):
    logger.info("Fetching base Minecraft version {}".format(version_id))
    version_manager = launcher.version_manager
    version_id = version_manager.resolve_version_name(version_id)
    versions_root = version_manager.versions_root
    vspec_path = versions_root / version_id / "{}.json".format(version_id)

    manifest_entry = None
    try:
        manifest_entry = next(
            entry
            for entry in version_manager.manifest["versions"]
            if entry["id"] == version_id
        )
    except (KeyError, StopIteration, RuntimeError, requests.RequestException):
        pass

    if manifest_entry is None:
        if vspec_path.exists():
            with open(vspec_path) as fd:
                return json.load(fd)
        raise MMCError(
            "Base Minecraft version '{}' is not available.".format(version_id)
        )

    sha1 = manifest_entry["sha1"]
    if vspec_path.exists() and file_sha1(vspec_path) == sha1:
        with open(vspec_path) as fd:
            return json.load(fd)

    try:
        response = requests.get(manifest_entry["url"], timeout=10)
        response.raise_for_status()
        raw = response.content
        if hashlib.sha1(raw).hexdigest() != sha1:
            raise MMCError(
                "The base version JSON failed hash verification."
            )
        vspec_path.parent.mkdir(parents=True, exist_ok=True)
        vspec_path.write_bytes(raw)
        return json.loads(raw)
    except requests.RequestException as e:
        if vspec_path.exists():
            logger.warning(
                "Failed to retrieve base version JSON ({}); using cache.".format(e)
            )
            with open(vspec_path) as fd:
                return json.load(fd)
        raise MMCError(
            "Failed to retrieve base version JSON: {}".format(e)
        )


def _java_component_for(major):
    if major <= 8:
        return "jre-legacy"
    if major == 16:
        return "java-runtime-alpha"
    if major == 17:
        return "java-runtime-gamma"
    if major == 18:
        return "java-runtime-beta"
    return "java-runtime-gamma"


def _parse_java_major(value):
    if not value:
        return None
    parts = value.split(".")
    try:
        if parts[0] == "1" and len(parts) > 1:
            return int(parts[1])
        return int(parts[0])
    except (TypeError, ValueError):
        return None


def _valid_name(name):
    return bool(name) and name not in (".", "..")


def _apply_java(vspec, patch):
    majors = patch.get("compatibleJavaMajors")
    if majors:
        major = max(int(m) for m in majors)
        component = patch.get("compatibleJavaName") or _java_component_for(major)
        vspec["javaVersion"] = {
            "component": component,
            "majorVersion": major,
        }
    elif "javaVersion" in patch:
        vspec["javaVersion"] = patch["javaVersion"]


def _apply_minecraft_patch(vspec, patch):
    """Apply the net.minecraft patch on top of the Mojang version JSON."""
    for key, value in patch.items():
        if key in IGNORED_PATCH_KEYS or key == "+libraries":
            continue
        if key == "libraries":
            vspec["libraries"] = list(value)
        elif key == "arguments":
            vspec["arguments"] = value
            vspec.pop("minecraftArguments", None)
        else:
            vspec[key] = value

    if "+libraries" in patch:
        vspec.setdefault("libraries", []).extend(patch["+libraries"])


def _merge_arguments(vspec, extra):
    if not isinstance(vspec.get("arguments"), dict):
        vspec["arguments"] = extra
        return
    for key in ("game", "jvm"):
        if key in extra:
            vspec["arguments"].setdefault(key, []).extend(extra[key])


def _apply_component_patch(vspec, patch):
    """Apply a non-Minecraft component (a loader, LWJGL, jarmod, ...)."""
    for key in ("libraries", "+libraries"):
        libraries = patch.get(key)
        if libraries:
            vspec.setdefault("libraries", []).extend(libraries)

    if "mainClass" in patch:
        vspec["mainClass"] = patch["mainClass"]
    if "minecraftArguments" in patch:
        vspec["minecraftArguments"] = patch["minecraftArguments"]
    if "arguments" in patch:
        _merge_arguments(vspec, patch["arguments"])

    for key in ("assets", "assetIndex", "releaseTime", "time", "type"):
        if key in patch:
            vspec[key] = patch[key]

    if "downloads" in patch:
        vspec.setdefault("downloads", {}).update(patch["downloads"])


def _component_main_jar(components):
    for component in components:
        main_jar = component.patch.get("mainJar")
        if not main_jar:
            continue
        artifact = main_jar.get("downloads", {}).get("artifact")
        if artifact:
            return artifact
    return None


def _collect_jar_mods(components):
    mods = []
    for component in components:
        for jar_mod in component.patch.get("jarMods") or []:
            mods.append((component, jar_mod))
    return mods


def _jar_mod_member(zf, pathmap, jar_mod):
    filename = jar_mod.get("MMC-filename")
    if not filename:
        filename = PurePosixPath(jar_mod.get("name", "mod.jar")).name

    member = _find_member(zf, pathmap, "jarmods/{}".format(filename))
    if member is None:
        matches = [
            name for name in zf.namelist() if PurePosixPath(name).name == filename
        ]
        if len(matches) == 1:
            member = matches[0]

    if member is None:
        raise MMCError(
            "Jar mod '{}' is not present in the pack.".format(filename)
        )
    return member


def _manifest_attributes(zf):
    try:
        data = zf.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except KeyError:
        return {}

    attributes = {}
    last_key = None
    for line in data.splitlines():
        if not line:
            break
        if line.startswith(" ") and last_key is not None:
            attributes[last_key] += line[1:]
            continue
        if ":" not in line:
            last_key = None
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        attributes[key] = value.strip()
        last_key = key
    return attributes


def _format_manifest(attributes):
    order = ["Manifest-Version", "Main-Class"]
    keys = [key for key in order if key in attributes]
    keys.extend(key for key in attributes if key not in order)
    lines = ["{}: {}".format(key, attributes[key]) for key in keys]
    return "\r\n".join(lines) + "\r\n\r\n"


def _is_signature(name):
    low = name.lower()
    return low.startswith("meta-inf/") and low.endswith(SIGNATURE_SUFFIXES)


def _merge_jars(base_jar, mod_jars, out_jar):
    """Merge jar mods over a base jar.

    Later jars win, and JAR signature files are dropped because modifying a
    signed jar invalidates them.  Returns the Main-Class found in the last
    manifest that declares one.
    """
    mod_names = []
    mod_manifest = {}
    for mod in mod_jars:
        with zipfile.ZipFile(mod) as zf:
            mod_names.append(set(zf.namelist()))
            mod_manifest.update(_manifest_attributes(zf))

    overridden = set()
    for names in mod_names:
        overridden |= names

    with zipfile.ZipFile(base_jar) as base, zipfile.ZipFile(
        out_jar, "w", zipfile.ZIP_DEFLATED
    ) as out:
        manifest_attrs = _manifest_attributes(base)
        manifest_attrs.update(mod_manifest)
        if "Manifest-Version" not in manifest_attrs:
            manifest_attrs["Manifest-Version"] = "1.0"

        main_class = manifest_attrs.get("Main-Class")
        out.writestr(
            "META-INF/MANIFEST.MF", _format_manifest(manifest_attrs)
        )
        written = {"META-INF/MANIFEST.MF"}

        def add_entries(source, skip):
            for info in source.infolist():
                name = info.filename
                if name in written or name in skip:
                    continue
                if name == "META-INF/MANIFEST.MF" or _is_signature(name):
                    continue
                out.writestr(info, source.read(info))
                written.add(name)

        add_entries(base, overridden)
        for index, mod in enumerate(mod_jars):
            later = set()
            for names in mod_names[index + 1 :]:
                later |= names
            with zipfile.ZipFile(mod) as zf:
                add_entries(zf, later)

    return main_class


def _download_base_jar(artifact, destination):
    url = artifact.get("url")
    if not url:
        raise MMCError("The pack's main jar has no download URL.")

    logger.info("Downloading base game jar")
    queue = DownloadQueue()
    queue.add(url, destination, artifact.get("size"))
    if not queue.download():
        raise MMCError("Failed to download the base game jar.")

    expected = artifact.get("sha1")
    if expected and file_sha1(destination) != expected:
        raise MMCError("The downloaded base game jar failed hash verification.")


def _uses_assets(vspec):
    data = json.dumps(
        {
            "minecraftArguments": vspec.get("minecraftArguments"),
            "arguments": vspec.get("arguments"),
        }
    )
    return any(placeholder in data for placeholder in ASSET_PLACEHOLDERS)


def _apply_assets(vspec, assets_root):
    """Keep Mojang assets if the arguments use them, otherwise disable them."""
    if _uses_assets(vspec):
        if "assetIndex" in vspec and not vspec.get("assets"):
            vspec["assets"] = vspec["assetIndex"]["id"]
        return

    vspec.pop("assetIndex", None)
    vspec["assets"] = EMPTY_ASSET_INDEX_ID

    index_dir = assets_root / "indexes"
    index_dir.mkdir(parents=True, exist_ok=True)
    index_path = index_dir / "{}.json".format(EMPTY_ASSET_INDEX_ID)
    if not index_path.exists() or index_path.read_bytes() != EMPTY_ASSET_INDEX:
        index_path.write_bytes(EMPTY_ASSET_INDEX)

    vspec["assetIndex"] = {
        "id": EMPTY_ASSET_INDEX_ID,
        "sha1": EMPTY_ASSET_INDEX_SHA1,
        "size": len(EMPTY_ASSET_INDEX),
        "url": "https://invalid.example/{}.json".format(EMPTY_ASSET_INDEX_ID),
    }


def _native_matches(name):
    parts = name.split(":")
    artifact = parts[1] if len(parts) > 1 else name
    if not any(artifact.endswith(suffix) for suffix in NATIVE_SUFFIXES):
        return True

    platform = osinfo.platform
    arch = osinfo.arch

    if platform == "linux":
        if arch == "arm64":
            return artifact.endswith("-natives-linux-arm64")
        if arch == "arm32":
            return artifact.endswith("-natives-linux-arm32")
        return artifact.endswith("-natives-linux")
    if platform == "windows":
        if arch == "arm64":
            return artifact.endswith("-natives-windows-arm64")
        if arch == "x86":
            return artifact.endswith("-natives-windows-x86")
        return artifact.endswith("-natives-windows")
    if platform == "osx":
        if arch == "arm64":
            return artifact.endswith("-natives-macos-arm64")
        return artifact.endswith("-natives-macos")

    # Unknown platform: leave the decision to the normal ruleset.
    return True


def _filter_native_libraries(libraries):
    result = []
    for library in libraries:
        name = library.get("name", "")
        if "-natives-" in name and not _native_matches(name):
            logger.debug("Skipping native library for another platform: {}".format(name))
            continue
        result.append(library)
    return result


def _dedupe_libraries(libraries):
    result = []
    seen = set()
    for library in libraries:
        name = library.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(library)
    return result


def _default_version_name(components):
    minecraft = next(
        (component for component in components if component.uid == "net.minecraft"),
        None,
    )
    mc_version = minecraft.version if minecraft else "minecraft"
    extras = [
        component
        for component in components
        if component.uid not in ("net.minecraft", "org.lwjgl")
        and not component.raw.get("dependencyOnly")
    ]

    if not extras:
        return sanitize_name("{}-mmc".format(mc_version))

    if len(extras) == 1:
        component = extras[0]
        version = sanitize_name(component.version)
        if component.uid.startswith("custom.jarmod."):
            name = component.uid[len("custom.jarmod.") :]
            return sanitize_name("{}-{}".format(name, version))
        if component.uid == "net.minecraftforge":
            return sanitize_name("{}-forge-{}".format(mc_version, version))
        if component.uid == "net.fabricmc.fabric-loader":
            return sanitize_name(
                "fabric-loader-{}-{}".format(version, mc_version)
            )
        if component.uid == "org.quiltmc.quilt-loader":
            return sanitize_name(
                "quilt-loader-{}-{}".format(version, mc_version))
        return sanitize_name(
            "{}-{}-{}".format(
                mc_version,
                component.uid.split(".")[-1],
                version,
            )
        )

    parts = [sanitize_name(mc_version)]
    for component in extras:
        parts.append(sanitize_name(component.uid.split(".")[-1]))
        parts.append(sanitize_name(component.version))
    return "-".join(part for part in parts if part)


def _default_instance_name(pack, version_name):
    name = pack.instance_cfg.get("name")
    if name:
        sanitized = sanitize_name(name)
        if sanitized:
            return sanitized
    return sanitize_name(version_name)


def _apply_instance_cfg(config, cfg, traits=()):
    if cfg.get("OverrideMemory", "true").lower() != "false":
        min_mem = cfg.get("MinMemAlloc")
        max_mem = cfg.get("MaxMemAlloc")
        if min_mem and min_mem.isdigit():
            config["java.memory.min"] = "{}M".format(min_mem)
        if max_mem and max_mem.isdigit():
            config["java.memory.max"] = "{}M".format(max_mem)

    jvm_args = cfg.get("JvmArgs")
    if jvm_args and cfg.get("OverrideJavaArgs", "true").lower() != "false":
        config["java.jvmargs"] = jvm_args

    if osinfo.platform == "osx" and "FirstThreadOnMacOS" in traits:
        jvm_args = config["java.jvmargs"]
        if "-XstartOnFirstThread" not in jvm_args:
            config["java.jvmargs"] = "{} -XstartOnFirstThread".format(
                jvm_args
            ).strip()


def _extract_game_files(pack, instance_root):
    count = 0
    with zipfile.ZipFile(pack.path) as zf:
        if pack.pathmap:
            entries = list(pack.pathmap.items())
        else:
            entries = [
                (info.filename, "/" + info.filename)
                for info in zf.infolist()
                if not info.is_dir()
            ]

        for member, destination in entries:
            parts = tuple(
                part
                for part in PurePosixPath(destination).parts
                if part not in ("", "/")
            )
            if not parts or parts[0] != "minecraft" or ".." in parts:
                continue

            relative = parts[1:]
            if not relative:
                continue

            try:
                info = zf.getinfo(member)
            except KeyError:
                logger.warning(
                    "File listed in pathmap is missing from the zip: {}".format(
                        member
                    )
                )
                continue
            if info.is_dir():
                continue

            outpath = instance_root.joinpath("minecraft", *relative)
            outpath.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, open(outpath, "wb") as destination_fd:
                shutil.copyfileobj(source, destination_fd)
            count += 1

    return count


def import_pack(
    launcher,
    pack_path,
    instance_name=None,
    version_name=None,
    create_instance=True,
):
    """Import a MultiMC/Prism export into zucaro."""
    pack = MMCPack.load(pack_path)
    components = pack.components
    minecraft = next(
        (component for component in components if component.uid == "net.minecraft"),
        None,
    )
    if minecraft is None:
        raise MMCError("The pack has no net.minecraft component.")

    logger.info("Importing {}".format(pack.path.name))

    raw_version_name = version_name or _default_version_name(components)
    version_name = sanitize_name(raw_version_name)
    if not _valid_name(version_name):
        raise MMCError("Invalid version name: {!r}".format(raw_version_name))
    versions_root = launcher.get_path(Directory.VERSIONS)
    version_dir = versions_root / version_name
    if version_dir.exists():
        raise VersionExistsError(version_name)

    real_instance_name = None
    if create_instance:
        if instance_name is not None:
            real_instance_name = sanitize_name(instance_name)
            if not _valid_name(real_instance_name):
                raise MMCError(
                    "Invalid instance name: {!r}".format(instance_name)
                )
        else:
            real_instance_name = _default_instance_name(pack, version_name)
            if not _valid_name(real_instance_name):
                real_instance_name = version_name
        if launcher.instance_manager.get_root(real_instance_name).exists():
            raise InstanceExistsError(real_instance_name)

    vspec = _get_base_vspec(launcher, minecraft.version)
    main_class_overridden = "mainClass" in minecraft.patch
    java_overridden = bool(
        minecraft.patch.get("compatibleJavaMajors")
        or "javaVersion" in minecraft.patch
    )
    _apply_minecraft_patch(vspec, minecraft.patch)
    _apply_java(vspec, minecraft.patch)

    for component in components:
        if component is minecraft:
            continue
        if (
            not component.patch
            and component.uid != "org.lwjgl"
            and not component.raw.get("dependencyOnly")
        ):
            logger.warning(
                "Component {} has no patch file; its changes were not "
                "imported.".format(component.uid)
            )
        if "mainClass" in component.patch:
            main_class_overridden = True
        if (
            component.patch.get("compatibleJavaMajors")
            or "javaVersion" in component.patch
        ):
            java_overridden = True
        _apply_component_patch(vspec, component.patch)
        _apply_java(vspec, component.patch)

    if not java_overridden:
        major = _parse_java_major(pack.instance_cfg.get("JavaVersion"))
        if major is not None:
            vspec["javaVersion"] = {
                "component": _java_component_for(major),
                "majorVersion": major,
            }

    _apply_assets(vspec, launcher.get_path(Directory.ASSETS))
    vspec["libraries"] = _dedupe_libraries(
        _filter_native_libraries(vspec.get("libraries", []))
    )

    jar_mods = _collect_jar_mods(components)
    main_artifact = _component_main_jar(components)
    base_download = vspec.get("downloads", {}).get("client")
    if main_artifact is None:
        main_artifact = base_download

    version_dir.mkdir()
    try:
        if jar_mods:
            if main_artifact is None:
                raise MMCError(
                    "The pack contains jar mods but no base game jar download."
                )

            with tempfile.TemporaryDirectory(
                prefix=".mmc-import-", dir=versions_root
            ) as tempdir:
                tempdir = Path(tempdir)
                base_jar = tempdir / "base.jar"
                _download_base_jar(main_artifact, base_jar)

                mod_paths = []
                with zipfile.ZipFile(pack.path) as zf:
                    for index, (_, jar_mod) in enumerate(jar_mods):
                        member = _jar_mod_member(zf, pack.pathmap, jar_mod)
                        mod_path = tempdir / "mod-{}.jar".format(index)
                        with zf.open(member) as source, open(
                            mod_path, "wb"
                        ) as destination_fd:
                            shutil.copyfileobj(source, destination_fd)
                        mod_paths.append(mod_path)

                merged_jar = tempdir / "merged.jar"
                merged_main_class = _merge_jars(
                    base_jar, mod_paths, merged_jar
                )
                shutil.copyfile(
                    merged_jar, version_dir / "{}.jar".format(version_name)
                )

            if merged_main_class and not main_class_overridden:
                vspec["mainClass"] = merged_main_class

            downloads = vspec.get("downloads")
            if downloads:
                downloads.pop("client", None)
                if not downloads:
                    vspec.pop("downloads", None)
        elif main_artifact is not None:
            vspec.setdefault("downloads", {})["client"] = main_artifact

        vspec["id"] = version_name
        vspec.pop("inheritsFrom", None)
        if jar_mods or main_artifact is not None:
            vspec.pop("jar", None)

        if not vspec.get("mainClass"):
            raise MMCError("Could not determine the main class.")

        with open(version_dir / "{}.json".format(version_name), "w") as fd:
            json.dump(vspec, fd, indent=2)
    except Exception:
        shutil.rmtree(version_dir, ignore_errors=True)
        raise

    logger.info("Installed version {}".format(version_name))
    logger.debug("Version directory: {}".format(version_dir))

    if create_instance:
        traits = set()
        for component in components:
            traits.update(component.patch.get("+traits") or [])
        instance = launcher.instance_manager.create(real_instance_name, version_name)
        try:
            _apply_instance_cfg(instance.config, pack.instance_cfg, traits)
            file_count = _extract_game_files(pack, instance.get_relpath())
            instance.config.save()
        except Exception:
            launcher.instance_manager.delete(real_instance_name)
            raise
        logger.info(
            "Created instance {} ({} game files)".format(
                real_instance_name, file_count
            )
        )

    return ImportResult(
        version_name=version_name,
        version_dir=version_dir,
        instance_name=real_instance_name,
    )
