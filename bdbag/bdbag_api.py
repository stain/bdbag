import os
import errno
import logging
import json
import shutil
import time
import datetime
import tempfile
import tarfile
import zipfile
import bagit
import bagit_profile
import bdbag
from collections import OrderedDict
from bdbag.fetch import fetcher
from bdbag.fetch.auth.keychain import DEFAULT_KEYCHAIN_FILE

logger = logging.getLogger(__name__)


def configure_logging(level=logging.INFO, logpath=None):
    logging.captureWarnings(True)
    log_format = "%(asctime)s - %(levelname)s - %(message)s"
    if logpath:
        logging.basicConfig(filename=logpath, level=level, format=log_format)
    else:
        logging.basicConfig(level=level, format=log_format)


def create_default_config():
    if not os.path.isdir(bdbag.DEFAULT_CONFIG_PATH):
        try:
            os.makedirs(bdbag.DEFAULT_CONFIG_PATH)
        except OSError as error:
            if error.errno != errno.EEXIST:
                raise
    with open(bdbag.DEFAULT_CONFIG_FILE, 'w') as cf:
        cf.write(json.dumps(bdbag.DEFAULT_CONFIG, sort_keys=True, indent=4, separators=(',', ': ')))
        cf.close()


def read_config(config_file, create_default=True):
    config = json.dumps(bdbag.DEFAULT_CONFIG)
    if config_file == bdbag.DEFAULT_CONFIG_FILE and not os.path.isfile(config_file) and create_default:
        logger.debug("No default configuration file found, attempting to create one.")
        try:
            create_default_config()
        except Exception as e:
            logger.debug("Unable to create default configuration file %s. Using internal defaults. %s" %
                         (bdbag.DEFAULT_CONFIG_FILE, bdbag.get_named_exception(e)))
    if os.path.isfile(config_file):
        with open(config_file) as cf:
            config = cf.read()

    return json.loads(config, object_pairs_hook=OrderedDict)


def read_metadata(metadata_file):
    if not metadata_file:
        return {}
    else:
        metadata_file = os.path.abspath(metadata_file)

    logger.info("Reading bag metadata from file %s" % metadata_file)
    with open(metadata_file) as mf:
        metadata = mf.read()
        mf.close()
        return json.loads(metadata, object_pairs_hook=OrderedDict)


def cleanup_bag(bag_path, save=False):
    logger.info("Cleaning up bag dir: %s" % bag_path)
    if save:
        saved_bag_path = ''.join([bag_path, '_', time.strftime("%Y-%m-%d_%H.%M.%S")])
        logger.info("Moving bag %s to %s" % (bag_path, saved_bag_path))
        shutil.move(bag_path, saved_bag_path)
    else:
        shutil.rmtree(bag_path)


def revert_bag(bag_path):
    if not is_bag(bag_path):
        logger.warn("Cannot revert the bag %s because it is not a bag directory!")
        return

    for path in os.listdir(bag_path):
        if os.path.basename(os.path.abspath(path)) != 'data':
            if path.startswith(("bag-info", "bagit", "fetch", "manifest-", "tagmanifest-")):
                os.remove(os.path.join(bag_path, path))

    data_path = os.path.join(bag_path, 'data')
    for path in os.listdir(data_path):
        old_path = os.path.join(data_path, path)
        new_path = os.path.join(bag_path, path)
        logger.debug("Bag revert: moving payload file %s to %s", old_path, new_path)
        os.rename(old_path, new_path)
    os.rmdir(data_path)


def prune_bag_manifests(bag):
    manifests_pruned = False
    manifests = list(bag.manifest_files())
    manifests += list(bag.tagmanifest_files())
    for manifest in manifests:
        if not manifest.find("tagmanifest-") is -1:
            search = "tagmanifest-"
        else:
            search = "manifest-"
        alg = os.path.basename(manifest).replace(search, "").replace(".txt", "")
        if alg not in bag.algs:
            logger.info("Removing unused manifest from bag: %s" % manifest)
            os.remove(manifest)
            manifests_pruned = True

    return manifests_pruned


def is_bag(bag_path):
    bag = None
    try:
        bag = bagit.Bag(bag_path)
    except (bagit.BagError, bagit.BagValidationError):
        pass
    return True if bag else False


def check_payload_consistency(bag, skip_remote=False, quiet=False):

    only_in_manifests, only_on_fs, only_in_fetch = bag.compare_manifests_with_fs_and_fetch()
    payload_consistent = not only_on_fs
    if not skip_remote:
        updated_remote_files = sorted(bag.remote_entries.keys())
        existing_remote_files = sorted(list(bag.files_to_be_fetched(False)))
        unresolved_fetch_files = set(bag.files_to_be_fetched()) - set(bag.payload_files())
        modified_remote_files = list(set(updated_remote_files) - set(existing_remote_files))
        normalized_updated_remote_files = set()
        for filename in updated_remote_files:
            normalized_updated_remote_files.add(os.path.normpath(filename))
        unresolved_manifest_files = list(set(only_in_manifests) - normalized_updated_remote_files)
        if modified_remote_files or only_in_fetch:
            payload_consistent = False
        if unresolved_manifest_files:
            payload_consistent = False
        if unresolved_fetch_files:
            payload_consistent = False
        for url, size, path in bag.fetch_entries():
            output_path = os.path.normpath(os.path.join(bag.path, path))
            if os.path.exists(output_path) and os.path.getsize(output_path) != int(size):
                payload_consistent = False
    elif payload_consistent:
        payload_consistent = not only_in_manifests

    for path in only_in_manifests:
        e = bagit.FileMissing(path)
        if not quiet:
            logger.warning(
                "%s. Resolve this file reference by either 1) adding the missing file to the bag payload or 2) adding "
                "a remote file reference in fetch.txt. or 3) re-run with the \"update\" flag set in order to remove "
                "this file from the bag manifest." % bdbag.get_named_exception(e))
    for path in only_on_fs:
        e = bagit.UnexpectedFile(path)
        if not quiet:
            logger.warning(
                "%s. Re-run with the \"update\" flag set in order to add this file to the manifest."
                % bdbag.get_named_exception(e))
    if not skip_remote:
        for path in only_in_fetch:
            e = bagit.UnexpectedRemoteFile(path)
            if not quiet:
                logger.warning(
                    "%s. Ensure that any remote file references from fetch.txt are also present in the manifest and "
                    "re-run with the \"update\" flag set in order to apply this change." % bdbag.get_named_exception(e))

    return payload_consistent


def should_update_manifests(bag, bag_algorithms, prune_manifests, remote_file_manifest):
    save_manifests = False

    if not prune_manifests:
        save_manifests = not all(x in bag.algs for x in bag_algorithms)
        if save_manifests:
            bag.algs = list(set(bag.algs).union(bag_algorithms))
    else:
        bag.algs = bag_algorithms
    if remote_file_manifest:
        bag.remote_entries.update(
            generate_remote_files_from_manifest(remote_file_manifest, bag.algs))
    skip_remote = True if not remote_file_manifest else False
    if prune_manifests:
        save_manifests = prune_bag_manifests(bag)
    if not save_manifests:
        save_manifests = not check_payload_consistency(bag, skip_remote, quiet=True)

    return save_manifests


def make_bag(bag_path,
             algs=None,
             update=False,
             save_manifests=True,
             prune_manifests=False,
             metadata=None,
             metadata_file=None,
             remote_file_manifest=None,
             config_file=bdbag.DEFAULT_CONFIG_FILE):
    bag = None
    try:
        bag = bagit.Bag(bag_path)
    except (bagit.BagError, bagit.BagValidationError):
        pass

    config = read_config(config_file)
    bag_config = config['bag_config']

    bag_algorithms = algs if algs else bag_config.get('bag_algorithms', ['md5', 'sha256'])
    bag_processes = bag_config.get('bag_processes', 1)

    # bag metadata merge order: config(if new, else if update use existing)->metadata_file->metadata
    if not update:
        bag_metadata = bag_config.get('bag_metadata', {}).copy()
    else:
        bag_metadata = bag.info
    bag_metadata.update(read_metadata(metadata_file))
    if metadata:
        bag_metadata.update(metadata)

    if 'Bagging-Date' not in bag_metadata:
        bag_metadata['Bagging-Date'] = datetime.date.strftime(datetime.date.today(), "%Y-%m-%d")

    if 'Bag-Software-Agent' not in bag_metadata:
        bag_metadata['Bag-Software-Agent'] = 'bdbag.py <http://github.com/ini-bdds/bdbag>'

    if bag:
        if update:
            try:
                logger.info("Updating bag: %s" % bag_path)
                bag.info.update(bag_metadata)
                manifests_update = should_update_manifests(bag, bag_algorithms, prune_manifests, remote_file_manifest)
                if manifests_update and not save_manifests:
                    logger.warn("Manifests must be updated due to bag payload change or checksum configuration change.")
                    save_manifests = True
                bag.save(bag_processes, manifests=save_manifests)
            except Exception as e:
                logger.error("Exception while updating bag manifests: %s", e)
                raise e
        else:
            logger.info("The directory %s is already a bag." % bag_path)

    else:
        remote_files = None
        if remote_file_manifest:
            remote_files = generate_remote_files_from_manifest(remote_file_manifest, bag_algorithms)
        bag = bagit.make_bag(bag_path, bag_metadata, bag_processes, bag_algorithms, remote_files)
        logger.info('Created bag: %s' % bag_path)

    return bag


def archive_bag(bag_path, bag_archiver):
    bag_archiver = bag_archiver.lower()

    try:
        logger.info("Verifying bag structure: %s" % bag_path)
        bag = bagit.Bag(bag_path)
        if not check_payload_consistency(bag, skip_remote=True):
            raise RuntimeError("Inconsistent payload state.")
    except Exception as e:
        logger.error("Error while archiving bag: %s", e)
        raise e

    logger.info("Archiving bag (%s): %s" % (bag_archiver, bag_path))
    tarmode = None
    archive = None
    fn = '.'.join([os.path.basename(bag_path), bag_archiver])
    if bag_archiver == 'tar':
        tarmode = 'w'
    elif bag_archiver == 'tgz':
        tarmode = 'w:gz'
    elif bag_archiver == 'bz2':
        tarmode = 'w:bz2'
    elif bag_archiver == 'zip':
        zfp = os.path.join(os.path.dirname(bag_path), fn)
        zf = zipfile.ZipFile(zfp, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True)
        for dirpath, dirnames, filenames in os.walk(bag_path):
            for name in filenames:
                filepath = os.path.normpath(os.path.join(dirpath, name))
                relpath = os.path.relpath(filepath, os.path.dirname(bag_path))
                if os.path.isfile(filepath):
                    zf.write(filepath, relpath)
        zf.close()
        archive = zf.filename
    else:
        raise RuntimeError("Archive format not supported for bag file: %s \n "
                           "Supported archive formats are ZIP or TAR/GZ/BZ2" % bag_path)

    if tarmode:
        tfp = os.path.join(os.path.dirname(bag_path), fn)
        t = tarfile.open(tfp, tarmode)
        t.add(bag_path, os.path.relpath(bag_path, os.path.dirname(bag_path)), recursive=True)
        t.close()
        archive = t.name

    logger.info('Created bag archive: %s' % archive)

    return archive


def extract_bag(bag_path, output_path=None, temp=False):
    if not os.path.exists(bag_path):
        raise RuntimeError("Specified bag path not found: %s" % bag_path)

    if os.path.isfile(bag_path):
        if temp:
            output_path = tempfile.mkdtemp(prefix='bag_')
        elif not output_path:
            output_path = os.path.splitext(bag_path)[0]
            if os.path.exists(output_path):
                newpath = ''.join([output_path, '-', time.strftime("%Y-%m-%d_%H_%M_%S")])
                logger.info("Specified output path %s already exists, moving existing directory to %s" %
                            (output_path, newpath))
                shutil.move(output_path, newpath)
        if zipfile.is_zipfile(bag_path):
            logger.info("Extracting ZIP archived bag file: %s" % bag_path)
            with open(bag_path, 'rb') as bag_file:
                zipped = zipfile.ZipFile(bag_file)
                zipped.extractall(output_path)
                zipped.close()
        elif tarfile.is_tarfile(bag_path):
            logger.info("Extracting TAR/GZ/BZ2 archived bag file: %s" % bag_path)
            tarred = tarfile.open(bag_path)
            tarred.extractall(output_path)
            tarred.close()
        else:
            raise RuntimeError("Archive format not supported for bag file: %s"
                               "\nSupported archive formats are ZIP or TAR/GZ/BZ2" % bag_path)

        for dirpath, dirnames, filenames in os.walk(output_path):
            if len(dirnames) > 1:
                # According to the spec there should only ever be one base bag directory at the base of a
                # deserialized archive. It is not clear if other non-bag directories are allowed.
                # For now, assume no other dirs allowed and terminate if more than one present.
                raise RuntimeError(
                    "Invalid bag serialization: Multiple base directories found in extracted archive.")
            else:
                output_path = os.path.abspath(os.path.join(dirpath, dirnames[0]))
                break

    logger.info("File %s was successfully extracted to directory %s" % (bag_path, output_path))

    return output_path


def validate_bag(bag_path, fast=False, callback=None, config_file=bdbag.DEFAULT_CONFIG_FILE):
    config = read_config(config_file)
    bag_config = config['bag_config']
    bag_processes = bag_config.get('bag_processes', 1)

    try:
        logger.info("Validating bag: %s" % bag_path)
        bag = bagit.Bag(bag_path)
        bag.validate(bag_processes if not callback else 1, fast=fast, callback=callback)
        logger.info("Bag %s is valid" % bag_path)
    except bagit.BagIncompleteError as e:
        logger.warning("BagIncompleteError: %s %s", e,
                       "This validation error may be transient if the bag contains unresolved remote file references "
                       "from a fetch.txt file. In this case the bag is incomplete but not necessarily invalid. "
                       "Resolve remote file references (if any) and re-validate.")
        raise e
    except bagit.BagValidationError as e:
        errors = list()
        for d in e.details:
            errors.append(bdbag.get_named_exception(d))
        raise bagit.BagValidationError('\nError: '.join(errors))
    except bagit.InterruptedError as e:
        logger.warn(bdbag.get_named_exception(e))
        raise e
    except Exception as e:
        raise RuntimeError("Unhandled exception while validating bag: %s" % e)


def validate_bag_profile(bag_path, profile_path=None):

    logger.info("Validating bag profile: %s" % bag_path)
    bag = bagit.Bag(bag_path)

    # Instantiate a profile, supplying its URI.
    if not profile_path:
        profile_path = bag.info.get(bdbag.BAG_PROFILE_TAG, None)
        if not profile_path:
            raise bagit_profile.ProfileValidationError("Bag does not contain a BagIt-Profile-Identifier")

    logger.info("Retrieving profile: %s" % profile_path)
    profile = bagit_profile.Profile(profile_path)

    # Validate the profile.
    if profile.validate(bag):
        logger.info("Bag structure conforms to specified profile")
    else:
        raise bagit_profile.ProfileValidationError("Bag structure does not conform to specified profile")

    return profile


def validate_bag_serialization(bag_path, bag_profile=None, bag_profile_path=None):

    if not bag_profile:
        if not bag_profile_path:
            raise bagit_profile.ProfileValidationError(
                "Unable to instantiate profile, no bag profile or profile path found")
        logger.info("Retrieving profile: %s" % bag_profile_path)
        bag_profile = bagit_profile.Profile(bag_profile_path)

    # Validate 'Serialization' and 'Accept-Serialization'.
    logger.info("Validating bag serialization: %s" % bag_path)
    try:
        bag_profile.validate_serialization(bag_path)
        logger.info("Bag serialization conforms to specified profile")
    except Exception as e:
        logger.error("Bag serialization does not conform to specified profile. Error: %s" % e)
        raise e


def generate_remote_files_from_manifest(remote_file_manifest, algs, strict=False):
    logger.info("Generating remote file references from %s" % remote_file_manifest)
    remote_files = dict()
    with open(remote_file_manifest, "r") as fetch_in:
        line = fetch_in.readline().lstrip()
        fetch_in.seek(0)
        is_json_stream = False
        if line.startswith('{'):
            fetch = fetch_in
            is_json_stream = True
        else:
            fetch = json.load(fetch_in, object_pairs_hook=OrderedDict)

        for entry in fetch:
            if is_json_stream:
                entry = json.loads(entry, object_pairs_hook=OrderedDict)

            entry['filename'] = ''.join(['data', '/', entry['filename']])

            add = True
            for alg in bagit.CHECKSUM_ALGOS:
                if alg in entry:
                    if strict and alg not in algs:
                        add = False
                    if add:
                        bagit.make_remote_file_entry(
                            remote_files, entry['filename'], entry['url'], entry['length'], alg, entry[alg])

        fetch_in.close()

    return remote_files


def resolve_fetch(bag_path, force=False, callback=None, keychain_file=DEFAULT_KEYCHAIN_FILE):
    bag = bagit.Bag(bag_path)
    if force or not check_payload_consistency(bag, skip_remote=False, quiet=True):
        logger.info("Attempting to resolve remote file references from fetch.txt...")
        return fetcher.fetch_bag_files(bag, keychain_file, force, callback)
    else:
        return True
