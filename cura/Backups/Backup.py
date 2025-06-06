# Copyright (c) 2025 UltiMaker
# Cura is released under the terms of the LGPLv3 or higher.
import tempfile

import json

import io
import os
import re
import shutil
from copy import deepcopy
from zipfile import ZipFile, ZIP_DEFLATED, BadZipfile
from typing import Callable, Dict, Optional, TYPE_CHECKING, List

from UM import i18nCatalog
from UM.Logger import Logger
from UM.Message import Message
from UM.Platform import Platform
from UM.PluginRegistry import PluginRegistry
from UM.Resources import Resources
from UM.Version import Version

if TYPE_CHECKING:
    from cura.CuraApplication import CuraApplication


class Backup:
    """The back-up class holds all data about a back-up.

    It is also responsible for reading and writing the zip file to the user data folder.
    """

    IGNORED_FILES = [r"cura\.log", r"plugins\.json", r"cache", r"__pycache__", r"\.qmlc", r"\.pyc"]
    """These files should be ignored when making a backup."""

    IGNORED_FOLDERS = []  # type: List[str]
    """These folders should be ignored when making a backup."""

    SECRETS_SETTINGS = ["general/ultimaker_auth_data"]
    """Secret preferences that need to obfuscated when making a backup of Cura"""

    catalog = i18nCatalog("cura")
    """Re-use translation catalog"""

    def __init__(self, application: "CuraApplication", zip_file: bytes = None, meta_data: Dict[str, str] = None) -> None:
        self._application = application
        self.zip_file = zip_file  # type: Optional[bytes]
        self.meta_data = meta_data  # type: Optional[Dict[str, str]]

    def makeFromCurrent(self, available_remote_plugins: frozenset[str] = frozenset()) -> None:
        """Create a back-up from the current user config folder."""

        cura_release = self._application.getVersion()
        version_data_dir = Resources.getDataStoragePath()

        Logger.log("d", "Creating backup for Cura %s, using folder %s", cura_release, version_data_dir)

        # obfuscate sensitive secrets
        secrets = self._obfuscate()

        # Ensure all current settings are saved.
        self._application.saveSettings()

        # We copy the preferences file to the user data directory in Linux as it's in a different location there.
        # When restoring a backup on Linux, we move it back.
        if Platform.isLinux(): #TODO: This should check for the config directory not being the same as the data directory, rather than hard-coding that to Linux systems.
            preferences_file_name = self._application.getApplicationName()
            preferences_file = Resources.getPath(Resources.Preferences, "{}.cfg".format(preferences_file_name))
            backup_preferences_file = os.path.join(version_data_dir, "{}.cfg".format(preferences_file_name))
            if os.path.exists(preferences_file) and (not os.path.exists(backup_preferences_file) or not os.path.samefile(preferences_file, backup_preferences_file)):
                Logger.log("d", "Copying preferences file from %s to %s", preferences_file, backup_preferences_file)
                shutil.copyfile(preferences_file, backup_preferences_file)

        # Create an empty buffer and write the archive to it.
        buffer = io.BytesIO()
        archive = self._makeArchive(buffer, version_data_dir, available_remote_plugins)
        if archive is None:
            return
        files = archive.namelist()

        # Count the metadata items. We do this in a rather naive way at the moment.
        machine_count = max(len([s for s in files if "machine_instances/" in s]) - 1, 0)  # If people delete their profiles but not their preferences, it can still make a backup, and report -1 profiles. Server crashes on this.
        material_count = max(len([s for s in files if "materials/" in s]) - 1, 0)
        profile_count = max(len([s for s in files if "quality_changes/" in s]) - 1, 0)
        plugin_count = len([s for s in files if "plugin.json" in s])
        # Store the archive and metadata so the BackupManager can fetch them when needed.
        self.zip_file = buffer.getvalue()
        self.meta_data = {
            "cura_release": cura_release,
            "machine_count": str(machine_count),
            "material_count": str(material_count),
            "profile_count": str(profile_count),
            "plugin_count": str(plugin_count)
        }
        # Restore the obfuscated settings
        self._illuminate(**secrets)

    def _fillToInstallsJson(self, file_path: str, reinstall_on_restore: frozenset[str], add_to_archive: Callable[[str, str], None]) -> Optional[str]:
        """ Moves all plugin-data (in a config-file) for plugins that could be (re)installed from the Marketplace from
            'installed' to 'to_installs' before adding that file to the archive.

        Note that the 'filename'-entry in the package-data (of the plugins) might not be valid anymore on restore.
        We'll replace it on restore instead, as that's the time when the new package is downloaded.

        :param file_path: Absolute path to the packages-file.
        :param reinstall_on_restore: A set of plugins that _can_ be reinstalled from the Marketplace.
        :param add_to_archive: A function/lambda that takes a filename and adds it to the archive (as the 2nd name).
        """
        with open(file_path, "r") as file:
            data = json.load(file)
            reinstall, keep_in = {}, {}
            for install_id, install_info in data["installed"].items():
                (reinstall if install_id in reinstall_on_restore else keep_in)[install_id] = install_info
            data["installed"] = keep_in
            data["to_install"].update(reinstall)
        if data is not None:
            tmpfile = tempfile.NamedTemporaryFile(delete_on_close=False)
            with open(tmpfile.name, "w") as outfile:
                json.dump(data, outfile)
            add_to_archive(tmpfile.name, file_path)
            return tmpfile.name
        return None

    def _findRedownloadablePlugins(self, available_remote_plugins: frozenset) -> (frozenset[str], frozenset[str]):
        """ Find all plugins that should be able to be reinstalled from the Marketplace.

        :param plugins_path: Path to all plugins in the user-space.
        :return: Tuple of a set of plugin-ids and a set of plugin-paths.
        """
        plugin_reg = PluginRegistry.getInstance()
        id = "id"
        plugins = [v for v in plugin_reg.getAllMetaData()
                   if v[id] in available_remote_plugins and not plugin_reg.isBundledPlugin(v[id])]
        return frozenset([v[id] for v in plugins]), frozenset([v["location"] for v in plugins])

    def _makeArchive(self, buffer: "io.BytesIO", root_path: str, available_remote_plugins: frozenset) -> Optional[ZipFile]:
        """Make a full archive from the given root path with the given name.

        :param root_path: The root directory to archive recursively.
        :return: The archive as bytes.
        """
        ignore_string = re.compile("|".join(self.IGNORED_FILES + self.IGNORED_FOLDERS))
        reinstall_instead_ids, reinstall_instead_paths = self._findRedownloadablePlugins(available_remote_plugins)
        tmpfiles = []
        try:
            archive = ZipFile(buffer, "w", ZIP_DEFLATED)
            add_path_to_archive = lambda path, alt_path: archive.write(path, alt_path[len(root_path) + len(os.sep):])
            for root, folders, files in os.walk(root_path, topdown=True):
                for item_name in folders + files:
                    absolute_path = os.path.join(root, item_name)
                    if ignore_string.search(absolute_path) or any([absolute_path.startswith(x) for x in reinstall_instead_paths]):
                        continue
                    if item_name == "packages.json":
                        tmpfiles.append(
                            self._fillToInstallsJson(absolute_path, reinstall_instead_ids, add_path_to_archive))
                    else:
                        add_path_to_archive(absolute_path, absolute_path)
            archive.close()
            for tmpfile_path in tmpfiles:
                try:
                    os.remove(tmpfile_path)
                except IOError as ex:
                    Logger.warning(f"Couldn't remove temporary file '{tmpfile_path}' because '{ex}'.")
            return archive
        except (IOError, OSError, BadZipfile) as error:
            Logger.log("e", "Could not create archive from user data directory: %s", error)
            self._showMessage(self.catalog.i18nc("@info:backup_failed",
                                                 "Could not create archive from user data directory: {}".format(error)),
                              message_type = Message.MessageType.ERROR)
            return None

    def _showMessage(self, message: str, message_type: Message.MessageType = Message.MessageType.NEUTRAL) -> None:
        """Show a UI message."""

        Message(message, title=self.catalog.i18nc("@info:title", "Backup"), message_type = message_type).show()

    def restore(self) -> bool:
        """Restore this back-up.

        :return: Whether we had success or not.
        """

        if not self.zip_file or not self.meta_data or not self.meta_data.get("cura_release", None):
            # We can restore without the minimum required information.
            Logger.log("w", "Tried to restore a Cura backup without having proper data or meta data.")
            self._showMessage(self.catalog.i18nc("@info:backup_failed",
                                                 "Tried to restore a Cura backup without having proper data or meta data."),
                              message_type = Message.MessageType.ERROR)
            return False

        current_version = Version(self._application.getVersion())
        version_to_restore = Version(self.meta_data.get("cura_release", "dev"))

        if current_version < version_to_restore:
            # Cannot restore version newer than current because settings might have changed.
            Logger.log("d", "Tried to restore a Cura backup of version {version_to_restore} with cura version {current_version}".format(version_to_restore = version_to_restore, current_version = current_version))
            self._showMessage(self.catalog.i18nc("@info:backup_failed",
                                                 "Tried to restore a Cura backup that is higher than the current version."),
                              message_type = Message.MessageType.ERROR)
            return False

        # Get the current secrets and store since the back-up doesn't contain those
        secrets = self._obfuscate()

        version_data_dir = Resources.getDataStoragePath()
        try:
            archive = ZipFile(io.BytesIO(self.zip_file), "r")
        except LookupError as e:
            Logger.log("d", f"The following error occurred while trying to restore a Cura backup: {str(e)}")
            Message(self.catalog.i18nc("@info:backup_failed",
                                       "The following error occurred while trying to restore a Cura backup:") + str(e),
                    title = self.catalog.i18nc("@info:title", "Backup"),
                    message_type = Message.MessageType.ERROR).show()

            return False
        extracted = self._extractArchive(archive, version_data_dir)

        # Under Linux, preferences are stored elsewhere, so we copy the file to there.
        if Platform.isLinux():
            preferences_file_name = self._application.getApplicationName()
            preferences_file = Resources.getPath(Resources.Preferences, "{}.cfg".format(preferences_file_name))
            backup_preferences_file = os.path.join(version_data_dir, "{}.cfg".format(preferences_file_name))
            Logger.log("d", "Moving preferences file from %s to %s", backup_preferences_file, preferences_file)
            try:
                shutil.move(backup_preferences_file, preferences_file)
            except EnvironmentError as e:
                Logger.error(f"Unable to back-up preferences file: {type(e)} - {str(e)}")

        # Read the preferences from the newly restored configuration (or else the cached Preferences will override the restored ones)
        self._application.readPreferencesFromConfiguration()

        # Restore the obfuscated settings
        self._illuminate(**secrets)

        return extracted

    def _extractArchive(self, archive: "ZipFile", target_path: str) -> bool:
        """Extract the whole archive to the given target path.

        :param archive: The archive as ZipFile.
        :param target_path: The target path.
        :return: Whether we had success or not.
        """

        # Implement security recommendations: Sanity check on zip files will make it harder to spoof.
        from cura.CuraApplication import CuraApplication
        config_filename = CuraApplication.getInstance().getApplicationName() + ".cfg"  # Should be there if valid.
        if config_filename not in [file.filename for file in archive.filelist]:
            Logger.logException("e", "Unable to extract the backup due to corruption of compressed file(s).")
            return False

        Logger.log("d", "Removing current data in location: %s", target_path)
        Resources.factoryReset()
        Logger.log("d", "Extracting backup to location: %s", target_path)
        name_list = archive.namelist()
        ignore_string = re.compile("|".join(self.IGNORED_FILES + self.IGNORED_FOLDERS))
        for archive_filename in name_list:
            if ignore_string.search(archive_filename):
                Logger.warning(f"File ({archive_filename}) in archive that doesn't fit current backup policy; ignored.")
                continue
            try:
                archive.extract(archive_filename, target_path)
            except (PermissionError, EnvironmentError):
                Logger.logException("e", f"Unable to extract the file {archive_filename} from the backup due to permission or file system errors.")
            except UnicodeEncodeError:
                Logger.error(f"Unable to extract the file {archive_filename} because of an encoding error.")
            CuraApplication.getInstance().processEvents()
        return True

    def _obfuscate(self) -> Dict[str, str]:
        """
        Obfuscate and remove the secret preferences that are specified in SECRETS_SETTINGS

        :return: a dictionary of the removed secrets. Note: the '/' is replaced by '__'
        """
        preferences = self._application.getPreferences()
        secrets = {}
        for secret in self.SECRETS_SETTINGS:
            secrets[secret.replace("/", "__")] = deepcopy(preferences.getValue(secret))
            preferences.setValue(secret, None)
        self._application.savePreferences()
        return secrets

    def _illuminate(self, **kwargs) -> None:
        """
        Restore the obfuscated settings

        :param kwargs: a dict of obscured preferences. Note: the '__' of the keys will be replaced by '/'
        """
        preferences = self._application.getPreferences()
        for key, value in kwargs.items():
            preferences.setValue(key.replace("__", "/"), value)
        self._application.savePreferences()
