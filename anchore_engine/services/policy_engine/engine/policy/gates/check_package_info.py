import enum
from anchore_engine.services.policy_engine.engine.policy.gate import BaseTrigger, Gate
from anchore_engine.services.policy_engine.engine.policy.utils import NameVersionListValidator, CommaDelimitedStringListValidator, barsplit_comma_delim_parser, delim_parser
from anchore_engine.db import ImagePackage, AnalysisArtifact, ImagePackageManifestEntry
from anchore_engine.services.policy_engine.engine.util.packages import compare_package_versions
from anchore_engine.services.policy_engine.engine.logs import get_logger

log = get_logger()


class VerifyTrigger(BaseTrigger):
    __trigger_name__ = 'VERIFY'
    __description__ = 'Check package integrity against package db in in the image. Triggers for changes or removal or content in all or the selected DIRS param if provided, and can filter type of check with the CHECK_ONLY param'
    __params__ = {
        'PKGS': CommaDelimitedStringListValidator(),
        'DIRS': CommaDelimitedStringListValidator(),
        'CHECK_ONLY': CommaDelimitedStringListValidator()
    }

    analyzer_type = 'base'
    analyzer_id = 'file_package_verify'
    analyzer_artifact = 'distro.pkgfilemeta'

    class VerificationStates(enum.Enum):
        changed = 'changed'
        missing = 'missing'


    def evaluate(self, image_obj, context):
        pkg_names = delim_parser(self.eval_params.get('PKGS', ''))
        pkg_dirs = delim_parser(self.eval_params.get('DIRS', ''))
        checks = map(lambda x: x.lower(), delim_parser(self.eval_params.get('CHECK_ONLY', '')))

        outlist = list()
        imageId = image_obj.id
        modified = {}

        if image_obj.fs:
            extracted_files_json = image_obj.fs.files
        else:
            extracted_files_json = []

        if pkg_names:
            pkgs = image_obj.packages.filter(ImagePackage.name.in_(pkg_names)).all()
        else:
            pkgs = image_obj.packages.all()

        for pkg in pkgs:
            pkg_name = pkg.name
            records = []
            if pkg_dirs:
                # Filter the specified dirs
                for d in pkg_dirs:
                    records += pkg.pkg_db_entries.filter(ImagePackageManifestEntry.file_path.startswith(d))
            else:
                records = [x for x in pkg.pkg_db_entries.all()]

            for pkg_db_record in records:
                status = self._diff_pkg_meta_and_file(pkg_db_record, extracted_files_json.get(pkg_db_record.file_path))

                if status and (not checks or status.value in checks):
                    self._fire(msg="VERIFY check against package db for package '{}' failed on entry '{}' with status: '{}'".format(pkg_name, pkg_db_record.file_path, status.value))

            # for pkg_db_record in entries:
            #     #pkg_name = pkg_db_record.artifact_key
            #     #entries = pkg_db_record.json_value
            #     if not pkg_dirs:
            #         filtered_entries = entries.keys()
            #     else:
            #         filtered_entries = filter(lambda x: any(map(lambda y: x.startswith(y), pkg_dirs)), entries.keys())
            #
            #     for e in filtered_entries:
            #         status = self._verify_path(entries[e], extracted_files_json.get(e))
            #
            #         if status and (not checks or status.value in checks):
            #             self._fire(msg="VERIFY check against package db for package '{}' failed on entry '{}' with status: '{}'".format(pkg_name, e, status.value))

    @classmethod
    def _diff_pkg_meta_and_file(cls, meta_db_entry, fs_entry):
        """
        Given the db record and the fs record, return one of [False, 'changed', 'removed'] for the diff depending on the diff detected.

        If entries are identical, return False since there is no diff.
        If there isa difference return a VerificationState.

        fs_entry is a dict expected to have the following keys:
        sha256_checksum
        md5_checksum
        sha1_checksum (expected but not required)
        mode - integer converted from the octal mode string
        size - integer size of the file

        :param meta_db_entry: An ImagePackageManifestEntry object built from the pkg db in the image indicating the expected state of the file
        :param fs_entry: A dict with metadata detected from image analysis
        :return: one of [False, <VerificationStates>]
        """

        # The fs record is None or empty
        if meta_db_entry and not fs_entry:
            return VerifyTrigger.VerificationStates.missing

        # This is unexpected
        if (fs_entry and not meta_db_entry) or fs_entry.get('name') != meta_db_entry.file_path:
            return False

        if meta_db_entry.is_config_file:
            return False # skip checks on config files if the flag is set

        # Store type of file
        fs_type = fs_entry.get('entry_type')

        # Check checksums
        if fs_type in ['file']:
            fs_digest = None
            if meta_db_entry.digest_algorithm == 'sha256':
                fs_digest = fs_entry.get('sha256_checksum')
            elif meta_db_entry.digest_algorithm == 'md5':
                fs_digest = fs_entry.get('md5_checksum')
            elif meta_db_entry.digest_algorithm == 'sha1':
                fs_digest = fs_entry.get('sha1_checksum')

            if meta_db_entry.digest and fs_digest and fs_digest != meta_db_entry.digest:
                return VerifyTrigger.VerificationStates.changed

        # Check mode
        if fs_type in ['file', 'dir']:
            fs_mode = fs_entry.get('mode')
            if meta_db_entry.mode and fs_mode:
                # Convert to octal for consistent checks
                oct_fs_mode = oct(fs_mode)
                oct_db_mode = oct(meta_db_entry.mode)

                # Trim mismatched lengths in octal mode
                if len(oct_db_mode) < len(oct_fs_mode):
                    oct_fs_mode = oct_fs_mode[-len(oct_db_mode):]
                elif len(oct_db_mode) > len(oct_fs_mode):
                    oct_db_mode = oct_db_mode[-len(oct_fs_mode):]

                if oct_db_mode != oct_fs_mode:
                    return VerifyTrigger.VerificationStates.changed

        if fs_type in ['file']:
            # Check size (Checksum should handle this)
            db_size = meta_db_entry.size
            fs_size = int(fs_entry.get('size'))
            if fs_size and db_size and fs_size != db_size:
                return VerifyTrigger.VerificationStates.changed

        # No changes or not enough data to compare
        return False


class PkgNotPresentTrigger(BaseTrigger):
    __trigger_name__ = 'PKGNOTPRESENT'
    __description__ = 'triggers if the package(s) specified in the params are not installed in the container image.  PKGFULLMATCH param can specify an exact match (ex: "curl|7.29.0-35.el7.centos").  PKGNAMEMATCH param can specify just the package name (ex: "curl").  PKGVERSMATCH can specify a minimum version and will trigger if installed version is less than the specified minimum version (ex: zlib|0.2.8-r2)',
    __params__ = {
        'PKGFULLMATCH': NameVersionListValidator(),
        'PKGNAMEMATCH': CommaDelimitedStringListValidator(),
        'PKGVERSMATCH': NameVersionListValidator()
    }

    def evaluate(self, image_obj, context):
        fullmatch = barsplit_comma_delim_parser(self.eval_params.get('PKGFULLMATCH'))
        namematch = delim_parser(self.eval_params.get('PKGNAMEMATCH'))
        vermatch = barsplit_comma_delim_parser(self.eval_params.get('PKGVERSMATCH'))

        outlist = list()
        imageId = image_obj.id

        names = set(fullmatch.keys()).union(set(namematch)).union(set(vermatch.keys()))
        if not names:
            return

        # Filter is possible since the lazy='dynamic' is set on the packages relationship in Image.
        for img_pkg in image_obj.packages.filter(ImagePackage.name.in_(names)).all():
            if img_pkg.name in fullmatch:
                if img_pkg.fullversion != fullmatch.get(img_pkg.name):
                    # Found but not right version
                    self._fire(msg="PKGNOTPRESENT input package (" + str(img_pkg.name) + ") is present (" + str(
                            img_pkg.fullversion) + "), but not at the version specified in policy (" + str(
                            fullmatch[img_pkg.name]) + ")")
                    fullmatch.pop(img_pkg.name)  # Assume only one version of a given package name is installed
                else:
                    # Remove it from the list
                    fullmatch.pop(img_pkg.name)

            # Name match is sufficient
            if img_pkg.name in namematch:
                namematch.remove(img_pkg.name)

            if img_pkg.name in vermatch:
                if img_pkg.fullversion != vermatch[img_pkg.name]:
                    # Check if version is less than param value
                    if compare_package_versions(img_pkg.distro_namespace_meta.flavor, img_pkg.name, img_pkg.version, img_pkg.name, vermatch[img_pkg.name]) < 0:
                        self._fire(msg="PKGNOTPRESENT input package (" + str(img_pkg.name) + ") is present (" + str(
                            img_pkg.fullversion) + "), but is lower version than what is specified in policy (" + str(
                            vermatch[img_pkg.name]) + ")")

                vermatch.pop(img_pkg.name)

        # Any remaining
        for pkg, version in fullmatch.items():
            self._fire(msg="PKGNOTPRESENT input package (" + str(pkg) + "-" + str(version) + ") is not present in container image")

        for pkg, version in vermatch.items():
            self._fire(msg="PKGNOTPRESENT input package (" + str(pkg) + "-" + str(
                version) + ") is not present in container image")

        for pkg in namematch:
            self._fire(msg="PKGNOTPRESENT input package (" + str(pkg) + ") is not present in container image")


class PackageCheckGate(Gate):
    __gate_name__ = 'PKGCHECK'
    __triggers__ = [
        PkgNotPresentTrigger,
        VerifyTrigger,
    ]
