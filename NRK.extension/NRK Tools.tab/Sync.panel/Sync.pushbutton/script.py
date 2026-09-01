# -*- coding: utf-8 -*-
"""
Generic Sync/Update button for pyRevit extensions.

This file is meant to be copied as-is into every extension that wants
online updates. The only thing that changes per extension is
"sync_config.json", which must sit next to this script's extension root
(one folder that ends in ".extension").

sync_config.json format:
{
    "repo": "githubuser/reponame",
    "branch": "main",
    "extension_folder": "NRK.extension"
}
"""
from __future__ import print_function

import os
import json
import shutil
import stat
import time
import traceback
import zipfile

try:
    import urllib2
except ImportError:
    # IronPython 3 / CPython 3 expose urllib.request instead of urllib2.
    from urllib import request as urllib2

from pyrevit.loader import sessionmgr
from pyrevit import forms

FILE_OPERATION_RETRIES = 6
FILE_OPERATION_RETRY_SECONDS = 0.75
HTTP_USER_AGENT = "pyRevit-Sync/1.0"


def find_extension_root(start_path):
    """Walk up from start_path until we hit a folder ending in .extension."""
    current_path = start_path
    while not current_path.endswith(".extension"):
        parent_path = os.path.dirname(current_path)
        if parent_path == current_path:
            break
        current_path = parent_path
    return current_path


def get_extension_root():
    current_path = os.path.dirname(os.path.abspath(__file__))
    return find_extension_root(current_path)


def load_config(extension_root):
    config_path = os.path.join(extension_root, "sync_config.json")
    if not os.path.isfile(config_path):
        raise RuntimeError(
            "sync_config.json not found in {}. Every extension using this "
            "Sync button needs its own sync_config.json.".format(extension_root)
        )
    with open(config_path, "r") as config_file:
        return json.load(config_file)


def get_temp_path(file_name):
    temp_dir = os.environ.get("TEMP") or os.environ.get("TMP") or os.getcwd()
    return os.path.join(temp_dir, file_name)


def download_zip(repo, branch, dest_zip_path):
    url = "https://api.github.com/repos/{}/zipball/{}".format(repo, branch)
    request = urllib2.Request(url)
    request.add_header("User-Agent", HTTP_USER_AGENT)
    request.add_header("Accept", "application/vnd.github+json")
    response = urllib2.urlopen(request, timeout=30)
    try:
        with open(dest_zip_path, "wb") as zip_file:
            zip_file.write(response.read())
    finally:
        response.close()


def extract_zip(zip_path, extract_to):
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        invalid_file = zip_ref.testzip()
        if invalid_file:
            raise RuntimeError("Downloaded archive is corrupted.")
        zip_ref.extractall(extract_to)


def find_matching_folder(extracted_dir, extension_folder_name):
    """
    GitHub zipball extracts to a single top-level folder like
    "reponame-branch-sha1234". The extension folder we care about is
    one level inside that.
    """
    top_level_items = [
        os.path.join(extracted_dir, name)
        for name in os.listdir(extracted_dir)
        if os.path.isdir(os.path.join(extracted_dir, name))
    ]
    if len(top_level_items) != 1:
        raise RuntimeError("Unexpected archive structure after extraction.")

    repo_root = top_level_items[0]
    matched_path = os.path.join(repo_root, extension_folder_name)
    if not os.path.isdir(matched_path):
        raise RuntimeError(
            "Could not find '{}' inside the downloaded repository.".format(
                extension_folder_name
            )
        )
    return matched_path


def remove_readonly_path(function, target_path, exception_info):
    try:
        os.chmod(target_path, stat.S_IWRITE)
        function(target_path)
    except Exception:
        raise exception_info[1]


def copy_file_with_retries(source_path, target_path, skipped_files):
    parent_path = os.path.dirname(target_path)
    if not os.path.isdir(parent_path):
        try:
            os.makedirs(parent_path)
        except OSError:
            skipped_files.append((target_path, "Destination folder is not writable."))
            return False

    last_error = None
    for attempt in range(FILE_OPERATION_RETRIES):
        try:
            if os.path.isfile(target_path):
                os.chmod(target_path, stat.S_IWRITE)
            shutil.copy2(source_path, target_path)
            return True
        except Exception as exc:
            last_error = exc
            if attempt < FILE_OPERATION_RETRIES - 1:
                time.sleep(FILE_OPERATION_RETRY_SECONDS * (attempt + 1))

    skipped_files.append((target_path, str(last_error) or "File is locked."))
    return False


def copy_extension_contents(source_root, destination_root):
    """Copy every file from the downloaded folder over the local extension,
    skipping files that are locked instead of aborting the whole update."""
    copied_count = 0
    skipped_files = []

    for root_path, _dir_names, file_names in os.walk(source_root):
        relative_root = os.path.relpath(root_path, source_root)
        destination_path = (
            destination_root
            if relative_root == "."
            else os.path.join(destination_root, relative_root)
        )

        if not os.path.isdir(destination_path):
            try:
                os.makedirs(destination_path)
            except OSError:
                skipped_files.append(
                    (destination_path, "Destination folder is not writable.")
                )
                continue

        for file_name in file_names:
            source_path = os.path.join(root_path, file_name)
            target_path = os.path.join(destination_path, file_name)
            if copy_file_with_retries(source_path, target_path, skipped_files):
                copied_count += 1

    return copied_count, skipped_files


def sync_tools():
    extension_root = get_extension_root()

    try:
        config = load_config(extension_root)
        repo = config["repo"]
        branch = config.get("branch", "main")
        extension_folder = config["extension_folder"]
    except Exception as exc:
        forms.alert(
            "Sync could not start.\n\nReason: {}".format(exc),
            title="Sync",
            warn_icon=True,
        )
        return

    update_token = "{}_{}".format(os.getpid(), int(time.time()))
    temp_zip = get_temp_path("sync_{}.zip".format(update_token))
    temp_dir = get_temp_path("sync_extract_{}".format(update_token))

    try:
        download_zip(repo, branch, temp_zip)
        extract_zip(temp_zip, temp_dir)
        matched_folder = find_matching_folder(temp_dir, extension_folder)

        copied_count, skipped_files = copy_extension_contents(
            matched_folder, extension_root
        )

        if copied_count == 0:
            raise RuntimeError("No files were copied. Nothing was updated.")

        if skipped_files:
            forms.alert(
                "Sync finished, but {} file(s) could not be replaced "
                "because they were in use. Close Revit and run Sync again "
                "to fully update.".format(len(skipped_files)),
                title="Sync",
                warn_icon=True,
            )
        else:
            print("Sync complete: {} file(s) updated.".format(copied_count))

        sessionmgr.reload_pyrevit()

    except Exception as exc:
        forms.alert(
            "Sync failed.\n\nReason: {}".format(exc),
            title="Sync",
            warn_icon=True,
        )
        print(traceback.format_exc())

    finally:
        for path in (temp_zip, temp_dir):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, onerror=remove_readonly_path)
                elif os.path.isfile(path):
                    os.chmod(path, stat.S_IWRITE)
                    os.remove(path)
            except Exception:
                pass


if __name__ == "__main__":
    sync_tools()
