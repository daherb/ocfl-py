"""OCFL Storage Root library.

This code uses PyFilesystem (import fs) exclusively for access to files. This
should enable application beyond the operating system filesystem.
"""
import json
import logging
import re
import fs
from fs.copy import copy_dir

from .disposition import get_dispositor
from .namaste import find_namastes, Namaste
from .object import Object
from .pyfs import open_fs, ocfl_walk, ocfl_opendir
from .validator import Validator
from .validation_logger import ValidationLogger


class StoreException(Exception):
    """Exception class for OCFL Storage Root."""


class Store():
    """Class for handling OCFL Storage Root and include OCFL Objects."""

    def __init__(self, root=None, disposition=None, lax_digests=False):
        """Initialize OCFL Storage Root."""
        self.root = root
        self.disposition = disposition
        self.lax_digests = lax_digests
        self._dispositor = None
        #
        self.declaration_tvalue = 'ocfl_1.0'
        self.spec_file = 'ocfl_1.0.txt'
        self.layout_file = 'ocfl_layout.json'
        self.registered_extensions = [
            # '0002-flat-direct-storage-layout',  # not included because doesn't have config
            '0003-hash-and-id-n-tuple-storage-layout',
            '0004-hashed-n-tuple-storage-layout'
        ]
        #
        self.root_fs = None
        self.num_traversal_errors = 0
        self.extension = None
        self.description = None
        self.log = logging.getLogger("ocfl-store")
        self.num_objects = 0
        self.good_objects = 0

    def open_root_fs(self, create=False):
        """Open pyfs filesystem for this OCFL storage root."""
        try:
            self.root_fs = open_fs(self.root, create=create)
        except (fs.opener.errors.OpenerError, fs.errors.CreateFailed) as e:
            raise StoreException("Failed to open OCFL storage root filesystem '%s' (%s)" % (self.root, str(e)))

    @property
    def dispositor(self):
        """Instance of dispositor class.

        Lazily initialized.
        """
        if not self._dispositor:
            self._dispositor = get_dispositor(disposition=self.disposition)
        return self._dispositor

    def traversal_error(self, code, **kwargs):
        """Record error traversing OCFL storage root."""
        self.num_traversal_errors += 1
        if self.log is None:  # FIXME - What to do in non-validator context?
            args = ', '.join('{0}={1!r}'.format(k, v) for k, v in kwargs.items())
            self.log.error("Traversal error %s - %s", code, args)
        else:
            self.log.error(code, **kwargs)

    def object_path(self, identifier):
        """Path to OCFL object with given identifier relative to the OCFL storage root."""
        # Try to find the path by looking at all objects in the store
        paths = [o['path'] for o in self.list() if o['id'] == identifier]
        if len(paths) == 1:
            return paths[0]
        else:
            # If object does not exist in store create a new path using the dispositor
            return self.dispositor.identifier_to_path(identifier)

    def initialize(self):
        """Create and initialize a new OCFL storage root."""
        (parent, root_dir) = fs.path.split(self.root)
        parent_fs = open_fs(parent)
        if parent_fs.exists(root_dir):
            raise StoreException("OCFL storage root %s already exists, aborting!" % (self.root))
        self.root_fs = parent_fs.makedir(root_dir)
        self.log.debug("Created OCFL storage root at %s", self.root)
        # Create root declaration
        Namaste(d=0, content=self.declaration_tvalue).write(pyfs=self.root_fs)
        # Create a layout declaration
        if self.disposition is not None:
            with self.root_fs.open(self.layout_file, 'w') as fh:
                layout = {'extension': self.disposition,
                          'description': "Non-standard layout from ocfl-py disposition -- FIXME"}
                json.dump(layout, fh, sort_keys=True, indent=2)
        self.log.info("Created OCFL storage root %s", self.root)

    def check_root_structure(self):
        """Check the OCFL storage root structure.

        Assumed that self.root_fs filesystem is available. Raises
        StoreException if there is an error.
        """
        # Storage root declaration
        namastes = find_namastes(0, pyfs=self.root_fs)
        if len(namastes) == 0:
            raise StoreException("Storage root %s lacks required 0= declaration file" % (self.root))
        if len(namastes) > 1:
            raise StoreException("Storage root %s has more than one 0= style declaration file" % (self.root))
        if namastes[0].tvalue != self.declaration_tvalue:
            raise StoreException("Storage root %s declaration file not as expected, got %s" % (self.root, namastes[0].filename))
        if not namastes[0].content_ok(pyfs=self.root_fs):
            raise StoreException("Storage root %s required declaration file %s has invalid content" % (self.root, namastes[0].filename))
        # Specification file and layout file
        if self.root_fs.exists(self.spec_file) and not self.root_fs.isfile(self.spec_file):
            raise StoreException("Storage root %s includes a specification entry that isn't a file" % (self.root))
        self.extension, self.description = self.parse_layout_file()
        # Other files are allowed...
        return True

    def parse_layout_file(self):
        """Read and parse layout file in OCFL storage root.

        Returns:
          - (extension, description) strings on success,
          - (None, None) if there is now layout file (it is optional)
          - otherwise raises a StoreException.
        """
        if self.root_fs.exists(self.layout_file):
            try:
                with self.root_fs.open(self.layout_file) as fh:
                    layout = json.load(fh)
                if not isinstance(layout, dict):
                    raise StoreException("Storage root %s has layout file that isn't a JSON object" % (self.root))
                if ('extension' not in layout or not isinstance(layout['extension'], str)
                        or 'description' not in layout or not isinstance(layout['description'], str)):
                    raise StoreException("Storage root %s has layout file doesn't have required extension and description string entries" % (self.root))
                return layout['extension'], layout['description']
            except Exception as e:  # FIXME - more specific?
                raise StoreException("OCFL storage root %s has layout file that can't be read (%s)" % (self.root, str(e)))
        else:
            return None, None

    def object_paths(self):
        """Generate object paths for every obect in the OCFL storage root.

        Yields (dirpath) that is the path to the directory for each object
        located, relative to the OCFL storage root and without a preceding /.

        Will log any errors seen while traversing the directory tree under the
        storage root.
        """
        for (dirpath, dirs, files) in ocfl_walk(self.root_fs, is_storage_root=True):
            if dirpath == '/':
                if 'extensions' in dirs:
                    self.validate_extensions_dir()
                    dirs.remove('extensions')
                # Ignore any other files in storage root
            elif (len(dirs) + len(files)) == 0:
                self.traversal_error("E073", path=dirpath)
            elif len(files) == 0:
                pass  # Just an intermediate directory
            else:
                # Is this directory an OCFL object? Look for any 0= file.
                zero_eqs = [file for file in files if file.startswith('0=')]
                if len(zero_eqs) > 1:
                    self.traversal_error("E003d", path=dirpath)
                elif len(zero_eqs) == 1:
                    declaration = zero_eqs[0]
                    match = re.match(r'''0=ocfl_object_(\d+\.\d+)''', declaration)
                    if match and match.group(1) == '1.0':
                        yield dirpath.lstrip('/')
                    elif match:
                        self.traversal_error("E004a", path=dirpath, version=match.group(1))
                    else:
                        self.traversal_error("E004b", path=dirpath, declaration=declaration)
                else:
                    self.traversal_error("E072", path=dirpath)

    def validate_extensions_dir(self):
        """Validate content of extensions directory inside storage root.

        Validate the extensions directory by checking that there aren't any
        entries in the extensions directory that aren't directories themselves.
        Where there are extension directories they SHOULD be registered and
        this code relies up the registered_extensions property to list known
        storage root extensions.
        """
        for entry in self.root_fs.scandir('extensions'):
            if entry.is_dir:
                if entry.name not in self.registered_extensions:
                    self.log.warning('W901', entry=entry.name)  # FIXME - No good warning code in spec
            else:
                self.traversal_error('E086', entry=entry.name)

    def list(self):
        """List contents of this OCFL storage root."""
        self.open_root_fs()
        self.check_root_structure()
        self.num_objects = 0
        objs = []
        for dirpath in self.object_paths():
            with ocfl_opendir(self.root_fs, dirpath) as obj_fs:
                # Parse inventory to extract id
                id = Object(obj_fs=obj_fs).id_from_inventory()
                self.log.debug("%s -- id=%s" % (dirpath, id))
                self.num_objects += 1
                # FIXME - maybe do some more stuff in here
                objs.append({"id": id, "path": dirpath})
        self.log.info("Found %d OCFL Objects under root %s", self.num_objects, self.root)
        return objs

    def validate_hierarchy(self, validate_objects=True, check_digests=True, show_warnings=False):
        """Validate storage root hierarchy.

        Returns:
            num_objects - number of objects checked
            good_objects - number of objects checked that were found to be valid
        """
        num_objects = 0
        good_objects = 0
        for dirpath in self.object_paths():
            if validate_objects:
                validator = Validator(check_digests=check_digests,
                                      lax_digests=self.lax_digests,
                                      show_warnings=show_warnings)
                if validator.validate(ocfl_opendir(self.root_fs, dirpath)):
                    good_objects += 1
                else:
                    self.log.info("Object at %s in INVALID", dirpath)
                messages = validator.__str__(prefix='[[' + dirpath + ']]')  # FIXME - how to show warnings sensibly?
                if messages != '':
                    self.log.info(messages)
                num_objects += 1
        return num_objects, good_objects

    def validate(self, validate_objects=True, check_digests=True, show_warnings=False, show_errors=True, lang='en'):
        """Validate OCFL storage root and optionally all objects."""
        valid = True
        if self.log is None:
            self.log = ValidationLogger(show_warnings=show_warnings, show_errors=show_errors, lang=lang)
        self.open_root_fs()
        try:
            self.check_root_structure()
            self.log.info("Storage root structure is VALID")
        except StoreException as e:
            valid = False
            self.log.info("Storage root structure is INVALID (%s)", str(e))
        self.num_objects, self.good_objects = self.validate_hierarchy(validate_objects=validate_objects, check_digests=check_digests, show_warnings=show_warnings)
        if validate_objects:
            if self.good_objects == self.num_objects:
                self.log.info("Objects checked: %d / %d are VALID", self.good_objects, self.num_objects)
            else:
                valid = False
                self.log.info("Objects checked: %d / %d are INVALID", self.num_objects - self.good_objects, self.num_objects)
        else:
            self.log.info("Not checking OCFL objects")
        # print(str(self.log))
        if self.num_traversal_errors > 0:
            valid = False
            self.log.info("Encountered %d errors traversing storage root", self.num_traversal_errors)
        # FIXME - do some stuff in here
        if valid:
            self.log.info("Storage root %s is VALID", self.root)
        else:
            self.log.info("Storage root %s is INVALID", self.root)
        return valid

    def add(self, object_path):
        """Add pre-constructed object from object_path."""
        self.open_root_fs()
        self.check_root_structure()
        # Sanity check
        o = Object()
        o.open_fs(object_path)
        inventory = o.parse_inventory()
        identifier = inventory['id']
        # Now copy
        path = self.object_path(identifier)
        self.log.info("Copying from %s to %s", object_path, fs.path.join(self.root, path))
        try:
            copy_dir(o.obj_fs, '/', self.root_fs, path)
            self.log.info("Copied")
        except Exception as e:
            self.log.error("Copy failed: %s", str(e))
            raise StoreException("Add object failed!")
