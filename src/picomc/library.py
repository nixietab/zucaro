import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from platform import architecture
from string import Template
from typing import Optional, Dict
from collections import defaultdict

from picomc.logging import logger
from picomc.osinfo import osinfo


@dataclass
class Artifact:
    url: Optional[str]
    path: PurePosixPath
    sha1: Optional[str]
    size: Optional[int]
    filename: str

    @classmethod
    def from_json(cls, obj):
        path = None
        if "path" in obj:
            path = PurePosixPath(obj["path"])
        filename = None
        if path:
            filename = path.name
        return cls(
            url=obj.get("url", None),
            path=path,
            sha1=obj["sha1"],
            size=obj["size"],
            filename=filename,
        )

    @classmethod
    def make(cls, descriptor):
        descriptor, *ext = descriptor.split("@")
        ext = ext[0] if ext else "jar"
        group, art_id, version, *class_ = descriptor.split(":")
        classifier = None
        if class_:
            classifier = class_[0]
        group = group.replace(".", "/")
        v2 = "-".join([version] + ([classifier] if classifier else []))

        filename = f"{art_id}-{v2}.{ext}"
        path = PurePosixPath(group) / art_id / version / filename

        return cls(url=None, path=path, sha1=None, size=None, filename=filename)

    def get_localpath(self, base):
        return Path(base) / self.path


class Library:
    MOJANG_BASE_URL = "https://libraries.minecraft.net/"
    _loaded_artifacts: Dict[str, str] = defaultdict(dict)

    def __init__(self, json_lib):
        self.json_lib = json_lib
        self._populate()

    def _should_load_artifact(self, group_id: str, artifact_id: str, version: str) -> bool:
        key = f"{group_id}:{artifact_id}"
        loaded_version = Library._loaded_artifacts.get(key)
        if not loaded_version:
            Library._loaded_artifacts[key] = version
            return True
        if loaded_version == version:
            return True
        if group_id == "org.ow2.asm":
            logger.warning(
                f"Skipping duplicate ASM artifact {artifact_id} version {version} "
                f"(version {loaded_version} already loaded)"
            )
            return False
        if self._compare_versions(version, loaded_version) > 0:
            Library._loaded_artifacts[key] = version
            return True
        return False

    def _compare_versions(self, ver1: str, ver2: str) -> int:
        v1_parts = [int(x) for x in ver1.split('.')]
        v2_parts = [int(x) for x in ver2.split('.')]
        for i in range(max(len(v1_parts), len(v2_parts))):
            v1 = v1_parts[i] if i < len(v1_parts) else 0
            v2 = v2_parts[i] if i < len(v2_parts) else 0
            if v1 > v2:
                return 1
            if v1 < v2:
                return -1
        return 0

    def _populate(self):
        js = self.json_lib
        self.descriptor = js["name"]
        self.is_native = "natives" in js
        self.is_classpath = not (self.is_native or js.get("presenceOnly", False))
        self.base_url = js.get("url", Library.MOJANG_BASE_URL)
        self.available = True
        name_parts = self.descriptor.split(':')
        if len(name_parts) >= 3:
            group_id, artifact_id, version = name_parts[:3]
            if not self._should_load_artifact(group_id, artifact_id, version):
                self.available = False
                return
        self.native_classifier = None
        if self.is_native:
            try:
                classifier_tmpl = self.json_lib["natives"][osinfo.platform]
                arch = architecture()[0][:2]
                self.native_classifier = Template(classifier_tmpl).substitute(arch=arch)
                self.descriptor = self.descriptor + ":" + self.native_classifier
            except KeyError:
                logger.warning(
                    f"Native {self.descriptor} is not available "
                    f"for current platform {osinfo.platform}."
                )
                self.available = False
                return
        self.virt_artifact = Artifact.make(self.descriptor)
        self.virt_artifact.url = urllib.parse.urljoin(
            self.base_url, self.virt_artifact.path.as_posix()
        )
        self.artifact = self.resolve_artifact()
        self.filename = self.virt_artifact.filename
        self.path = self.virt_artifact.path
        if self.artifact:
            final_art = self.artifact
            if self.artifact.path is not None:
                assert self.virt_artifact.path == self.artifact.path
        else:
            final_art = self.virt_artifact
        self.url = final_art.url
        self.sha1 = final_art.sha1
        self.size = final_art.size

    def resolve_artifact(self):
        if self.is_native:
            if self.native_classifier is None:
                return None
            else:
                try:
                    art = self.json_lib["downloads"]["classifiers"][
                        self.native_classifier
                    ]
                    return Artifact.from_json(art)
                except KeyError:
                    return None
        else:
            try:
                return Artifact.from_json(self.json_lib["downloads"]["artifact"])
            except KeyError:
                return None

    def get_abspath(self, library_root):
        return self.virt_artifact.get_localpath(library_root)
