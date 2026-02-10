import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional

import requests

from zucaro.downloader import DownloadQueue
from zucaro.logging import logger
from zucaro.utils import Directory

ADOPTIUM_API_BASE = "https://api.adoptium.net/v3"

class JavaManager:
    def __init__(self, launcher):
        self.launcher = launcher
        self.java_dir = launcher.get_path(Directory.JAVA)
        self._ensure_java_dir()

    def _ensure_java_dir(self):
        """Ensure the Java directory exists."""
        os.makedirs(self.java_dir, exist_ok=True)

    def _get_system_info(self) -> Dict[str, str]:
        """Get system architecture and OS information."""
        system = platform.system().lower()
        machine = platform.machine().lower()

        # Map system architectures to Adoptium's naming
        arch_map = {
            'amd64': 'x64',
            'x86_64': 'x64',
            'arm64': 'aarch64',
            'aarch64': 'aarch64'
        }

        # Map OS names to Adoptium's naming
        os_map = {
            'windows': 'windows',
            'linux': 'linux',
            'darwin': 'mac'
        }

        arch = arch_map.get(machine, machine)
        os_name = os_map.get(system, system)

        return {
            'os': os_name,
            'architecture': arch
        }

    def _get_required_java_version(self, minecraft_version: str) -> str:
        """Get the required Java version by checking the version metadata."""
        version_obj = self.launcher.version_manager.get_version(minecraft_version)
        java_component = version_obj.java_version

        if not java_component:
            # Minecraft versions before 1.17 used Java 8
            return "8"

        # The java_version component contains the major version number
        # For example: { "component": "jre-legacy", "majorVersion": 8 }
        # or { "component": "java-runtime-alpha", "majorVersion": 16 }
        return str(java_component.get("majorVersion", 8))

    def _get_java_release(self, version: str) -> Dict:
        """Get the latest Java release information from Adoptium API."""
        sys_info = self._get_system_info()

        url = f"{ADOPTIUM_API_BASE}/assets/latest/{version}/hotspot"
        params = {
            'architecture': sys_info['architecture'],
            'image_type': 'jre',
            'os': sys_info['os'],
            'vendor': 'eclipse'
        }

        response = requests.get(url, params=params)
        response.raise_for_status()

        releases = response.json()
        if not releases:
            raise RuntimeError(f"No Java {version} release found for your system")

        return releases[0]

    def download_java(self, version: str) -> Path:
        """Download and install a specific Java version."""
        release = self._get_java_release(version)
        binary = release['binary']

        # Create version-specific directory
        java_path = self.java_dir / f"java{version}"
        if java_path.exists():
            logger.info(f"Java {version} is already installed at {java_path}")
            return java_path

        # Download the JRE
        logger.info(f"Downloading Java {version}...")
        dq = DownloadQueue()
        download_url = binary['package']['link']
        package_name = binary['package']['name']

        # Create a temporary directory for downloading and extraction
        with tempfile.TemporaryDirectory(dir=self.java_dir) as temp_dir:
            temp_path = Path(temp_dir)
            temp_file = temp_path / package_name

            # Download to temporary location
            dq.add(download_url, temp_file, binary['package']['size'])
            if not dq.download():
                raise RuntimeError(f"Failed to download Java {version}")

            # Extract the archive
            logger.info(f"Extracting Java {version}...")
            try:
                shutil.unpack_archive(temp_file, temp_path)

                # Find the bin directory
                bin_dirs = list(temp_path.rglob('bin'))
                if not bin_dirs:
                    raise RuntimeError("Could not find Java binary directory")

                # Get the root directory that contains the bin directory
                java_root = bin_dirs[0].parent

                # Move the extracted contents to the final location
                if not java_path.exists():
                    shutil.move(str(java_root), str(java_path))
                else:
                    # If the directory exists (race condition), use a unique name
                    new_path = java_path.with_suffix('.new')
                    shutil.move(str(java_root), str(new_path))
                    shutil.rmtree(java_path, ignore_errors=True)
                    new_path.rename(java_path)

            except Exception as e:
                # Clean up on failure
                if java_path.exists():
                    shutil.rmtree(java_path, ignore_errors=True)
                raise RuntimeError(f"Failed to extract Java {version}: {str(e)}")

        return java_path

    def get_java_path(self, minecraft_version: str) -> Optional[Path]:
        """Get the path to the appropriate Java version for a Minecraft version."""
        java_version = self._get_required_java_version(minecraft_version)
        logger.info(f"Minecraft {minecraft_version} requires Java {java_version}")
        java_path = self.java_dir / f"java{java_version}"

        if not java_path.exists():
            logger.info(f"Java {java_version} not found, downloading...")
            java_path = self.download_java(java_version)

        # Find java executable
        if sys.platform == 'win32':
            java_exe = java_path / 'bin' / 'java.exe'
        else:
            java_exe = java_path / 'bin' / 'java'

        if not java_exe.exists():
            raise RuntimeError(f"Java executable not found in {java_path}")

        return java_exe
