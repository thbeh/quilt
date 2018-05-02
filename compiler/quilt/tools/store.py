"""
Build: parse and add user-supplied files to store
"""
import os

from shutil import rmtree

from .const import DEFAULT_TEAM, PACKAGE_DIR_NAME, QuiltException
from .core import FileNode, RootNode, TableNode, find_object_hashes
from .package import Package, PackageException
from .util import BASE_DIR, sub_dirs, sub_files, is_nodename

CHUNK_SIZE = 4096

# Helper function to return the default package store path
def default_store_location():
    package_dir = os.path.join(BASE_DIR, PACKAGE_DIR_NAME)
    return os.getenv('QUILT_PRIMARY_PACKAGE_DIR', package_dir)

class StoreException(QuiltException):
    """
    Exception class for store I/O
    """
    pass


class PackageStore(object):
    """
    Base class for managing Quilt data package repositories. This
    class and its subclasses abstract file formats, file naming and
    reading and writing to/from data files.
    """
    BUILD_DIR = 'build'
    OBJ_DIR = 'objs'
    TMP_OBJ_DIR = 'tmp'
    PKG_DIR = 'pkgs'
    CACHE_DIR = 'cache'
    VERSION = '1.4'

    def __init__(self, location=None):
        if location is None:
            location = default_store_location()

        assert os.path.basename(os.path.abspath(location)) == PACKAGE_DIR_NAME, \
            "Unexpected package directory: %s" % location
        self._path = location

        version = self._read_format_version()

        if version == '1.2':
            # Migrate to the teams format.
            pkgdir = os.path.join(self._path, self.PKG_DIR)
            old_dirs = sub_dirs(pkgdir)
            os.mkdir(os.path.join(pkgdir, DEFAULT_TEAM))
            for old_dir in old_dirs:
                os.rename(os.path.join(pkgdir, old_dir), os.path.join(pkgdir, DEFAULT_TEAM, old_dir))
            version = '1.3'
            self._write_format_version(version)
            # Fall-through to the next migration.

        if version == '1.3':
            print("Migrating objects to new format... ", end='')
            for i in range(256):
                prefix_path = os.path.join(self.object_dir(), '%02x' % i)
                if not os.path.isdir(prefix_path):
                    os.mkdir(prefix_path)
            for obj in os.listdir(self.object_dir()):
                if len(obj) == 64:
                    os.rename(os.path.join(self.object_dir(), obj), self.object_path(obj))
            print("done.")
            version = '1.4'
            self._write_format_version(version)

        if version not in (None, self.VERSION):
            msg = (
                "The package repository at {0} is not compatible"
                " with this version of quilt. Revert to an"
                " earlier version of quilt or remove the existing"
                " package repository."
            )
            raise StoreException(msg.format(self._path))

    def create_dirs(self):
        """
        Creates the store directory and its subdirectories.
        """
        if not os.path.isdir(self._path):
            os.makedirs(self._path)
        for dir_name in [self.OBJ_DIR, self.TMP_OBJ_DIR, self.PKG_DIR, self.CACHE_DIR]:
            path = os.path.join(self._path, dir_name)
            if not os.path.isdir(path):
                os.mkdir(path)
        for i in range(256):
            prefix_path = os.path.join(self.object_dir(), '%02x' % i)
            if not os.path.isdir(prefix_path):
                os.mkdir(prefix_path)
        if not os.path.exists(self._version_path()):
            self._write_format_version(self.VERSION)

    @classmethod
    def find_store_dirs(cls):
        """
        Returns the primary package directory and any additional ones from QUILT_PACKAGE_DIRS.
        """
        store_dirs = [default_store_location()]
        extra_dirs_str = os.getenv('QUILT_PACKAGE_DIRS')
        if extra_dirs_str:
            store_dirs.extend(extra_dirs_str.split(':'))
        return store_dirs

    @classmethod
    def find_package(cls, team, user, package, store_dir=None):
        """
        Finds an existing package in one of the package directories.
        """
        cls.check_name(team, user, package)
        dirs = cls.find_store_dirs()
        for store_dir in dirs:
            store = PackageStore(store_dir)
            pkg = store.get_package(team, user, package)
            if pkg is not None:
                return pkg
        return None

    @classmethod
    def check_name(cls, team, user, package, subpath=None):
        if team is not None and not is_nodename(team):
            raise StoreException("Invalid team name: %r" % team)
        if not is_nodename(user):
            raise StoreException("Invalid user name: %r" % user)
        if not is_nodename(package):
            raise StoreException("Invalid package name: %r" % package)
        if subpath:
            for element in subpath:
                if not is_nodename(element):
                    raise StoreException("Invalid element in subpath: %r" % element)

    def _version_path(self):
        return os.path.join(self._path, '.format')

    def _read_format_version(self):
        if os.path.exists(self._version_path()):
            with open(self._version_path(), 'r') as versionfile:
                version = versionfile.read()
                return version
        else:
            return None

    def _write_format_version(self, version):
        with open(self._version_path(), 'w') as versionfile:
            versionfile.write(version)

    # TODO: find a package instance other than 'latest', e.g. by
    # looking-up by hash, tag or version in the local store.
    def get_package(self, team, user, package):
        """
        Gets a package from this store.
        """
        self.check_name(team, user, package)
        path = self.package_path(team, user, package)
        if os.path.isdir(path):
            try:
                return Package(
                    store=self,
                    user=user,
                    package=package,
                    path=path
                    )
            except PackageException:
                pass
        return None

    def install_package(self, team, user, package, contents):
        """
        Creates a new package in the default package store
        and allocates a per-user directory if needed.
        """
        self.check_name(team, user, package)
        assert contents is not None

        self.create_dirs()

        path = self.package_path(team, user, package)

        # Delete any existing data.
        try:
            os.remove(path)
        except OSError:
            pass

        return Package(
            store=self,
            user=user,
            package=package,
            path=path,
            contents=contents
        )

    def create_package(self, team, user, package, dry_run=False):
        """
        Creates a new package and initializes its contents. See `install_package`.
        """
        if dry_run:
            return Package(self, user, package, '.', RootNode(dict()))
        contents = RootNode(dict())
        return self.install_package(team, user, package, contents)

    def remove_package(self, team, user, package):
        """
        Removes a package (all instances) from this store.
        """
        self.check_name(team, user, package)

        path = self.package_path(team, user, package)
        remove_objs = set()
        # TODO: do we really want to delete invisible dirs?
        if os.path.isdir(path):
            # Collect objects from all instances for potential cleanup
            contents_path = os.path.join(path, Package.CONTENTS_DIR)
            for instance in os.listdir(contents_path):
                pkg = Package(self, user, package, path, pkghash=instance)
                remove_objs.update(find_object_hashes(pkg.get_contents()))
            # Remove package manifests
            rmtree(path)

        return self.prune(remove_objs)

    def iterpackages(self):
        """
        Return an iterator over all the packages in the PackageStore.
        """
        pkgdir = os.path.join(self._path, self.PKG_DIR)
        if not os.path.isdir(pkgdir):
            return
        for team in sub_dirs(pkgdir):
            for user in sub_dirs(self.team_path(team)):
                for pkg in sub_dirs(self.user_path(team, user)):
                    pkgpath = self.package_path(team, user, pkg)
                    for hsh in sub_files(os.path.join(pkgpath, Package.CONTENTS_DIR)):
                        yield Package(self, user, pkg, pkgpath, pkghash=hsh)

    def ls_packages(self):
        """
        List packages in this store.
        """
        packages = []
        pkgdir = os.path.join(self._path, self.PKG_DIR)
        if not os.path.isdir(pkgdir):
            return []
        for team in sub_dirs(pkgdir):
            for user in sub_dirs(self.team_path(team)):
                for pkg in sub_dirs(self.user_path(team, user)):
                    pkgpath = self.package_path(team, user, pkg)
                    pkgmap = {h : [] for h in sub_files(os.path.join(pkgpath, Package.CONTENTS_DIR))}
                    for tag in sub_files(os.path.join(pkgpath, Package.TAGS_DIR)):
                        with open(os.path.join(pkgpath, Package.TAGS_DIR, tag), 'r') as tagfile:
                            pkghash = tagfile.read()
                            pkgmap[pkghash].append(tag)
                    for pkghash, tags in pkgmap.items():
                        # add teams here if any other than DEFAULT_TEAM should be hidden.
                        team_token = '' if team in (DEFAULT_TEAM,) else team + ':'
                        fullpkg = "{team}{owner}/{pkg}".format(team=team_token, owner=user, pkg=pkg)
                        # Add an empty string tag for untagged hashes
                        displaytags = tags if tags else [""]
                        # Display a separate full line per tag like Docker
                        for tag in displaytags:
                            packages.append((fullpkg, str(tag), pkghash))

        return packages

    def team_path(self, team=None):
        """
        Returns the path to directory with the team's users' package repositories.
        """
        if team is None:
            team = DEFAULT_TEAM
        return os.path.join(self._path, self.PKG_DIR, team)

    def user_path(self, team, user):
        """
        Returns the path to directory with the user's package repositories.
        """
        return os.path.join(self.team_path(team), user)

    def package_path(self, team, user, package):
        """
        Returns the path to a package repository.
        """
        return os.path.join(self.user_path(team, user), package)

    def object_dir(self):
        """
        Returns the path to the object directory.
        """
        return os.path.join(self._path, self.OBJ_DIR)

    def object_path(self, objhash):
        """
        Returns the path to an object file based on its hash.
        """
        return os.path.join(self.object_dir(), objhash[:2], objhash[2:])

    def temporary_object_path(self, name):
        """
        Returns the path to a temporary object, before we know its hash.
        """
        return os.path.join(self._path, self.TMP_OBJ_DIR, name)

    def cache_path(self, name):
        """
        Returns the path to a temporary object, before we know its hash.
        """
        return os.path.join(self._path, self.CACHE_DIR, name)

    def prune(self, objs):
        """
        Clean up objects not referenced by any packages. Try to prune all
        objects by default.
        """
        remove_objs = set(objs)

        for pkg in self.iterpackages():
            remove_objs.difference_update(find_object_hashes(pkg.get_contents()))

        for obj in remove_objs:
            path = self.object_path(obj)
            if os.path.exists(path):
                os.remove(path)
        return remove_objs
