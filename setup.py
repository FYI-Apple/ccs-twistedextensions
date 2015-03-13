#!/usr/bin/env python

##
# Copyright (c) 2006-2015 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##

from __future__ import print_function

import os
from os.path import dirname, abspath, join as joinpath
import subprocess
import sys

import errno
from setuptools import setup, find_packages as setuptools_find_packages
from xml.etree import ElementTree

base_version = "0.1"


#
# Utilities
#
def find_packages():
    modules = [
        "twisted.plugins",
    ]

    def is_package(path):
        return (
            os.path.isdir(path) and
            os.path.isfile(os.path.join(path, "__init__.py"))
        )

    for pkg in filter(is_package, os.listdir(".")):
        modules.extend([pkg, ] + [
            "{}.{}".format(pkg, subpkg)
            for subpkg in setuptools_find_packages(pkg)
        ])
    return modules


def svn_info(wc_path):
    """
    Look up info on a Subversion working copy.
    """
    try:
        info_xml = subprocess.check_output(
            ["svn", "info", "--xml", wc_path],
            stderr=subprocess.STDOUT,
        )
    except OSError as e:
        if e.errno == errno.ENOENT:
            return None
        raise
    except subprocess.CalledProcessError:
        return None

    info = ElementTree.fromstring(info_xml)
    assert info.tag == "info"

    entry = info.find("entry")
    url = entry.find("url")
    root = entry.find("repository").find("root")
    if url.text.startswith(root.text):
        location = url.text[len(root.text):].strip("/")
    else:
        location = url.text.strip("/")
    project, branch = location.split("/")

    return dict(
        root=root.text,
        project=project, branch=branch,
        revision=info.find("entry").attrib["revision"],
    )


def svn_status(wc_path):
    """
    Look up status on a Subversion working copy.
    Complies with PEP 440: https://www.python.org/dev/peps/pep-0440/

    Examples:
        C{6.0} (release tag)
        C{6.1.b2.dev14564} (release branch)
        C{7.0.b1.dev14564} (trunk)
        C{6.0.a1.dev14441+branches.pg8000} (other branch)
    """
    try:
        status_xml = subprocess.check_output(
            ["svn", "status", "--xml", wc_path]
        )
    except OSError as e:
        if e.errno == errno.ENOENT:
            return
        raise
    except subprocess.CalledProcessError:
        return

    status = ElementTree.fromstring(status_xml)
    assert status.tag == "status"

    target = status.find("target")

    for entry in target.findall("entry"):
        entry_status = entry.find("wc-status")
        if entry_status is not None:
            item = entry_status.attrib["item"]
            if item == "unversioned":
                continue
        path = entry.attrib["path"]
        if wc_path != ".":
            path = path.lstrip(wc_path)
        yield dict(path=path)


def version():
    """
    Compute the version number.
    """
    source_root = dirname(abspath(__file__))

    info = svn_info(source_root)

    if info is None:
        # We don't have Subversion info...
        return "{}.a1+unknown".format(base_version)

    assert info["project"] == project_name, (
        "Subversion project {!r} != {!r}"
        .format(info["project"], project_name)
    )

    status = svn_status(source_root)

    for entry in status:
        # We have modifications.
        modified = "+modified"
        break
    else:
        modified = ""


    if info["branch"].startswith("tags/release/"):
        project_version = info["branch"].lstrip("tags/release/")
        project, version = project_version.split("-")
        assert project == project_name, (
            "Tagged project {!r} != {!r}".format(project, project_name)
        )
        assert version == base_version, (
            "Tagged version {!r} != {!r}".format(version, base_version)
        )
        # This is a correctly tagged release of this project.
        return "{}{}".format(base_version, modified)

    if info["branch"].startswith("branches/release/"):
        project_version = info["branch"].lstrip("branches/release/")
        project, version, dev = project_version.split("-")
        assert project == project_name, (
            "Branched project {!r} != {!r}".format(project, project_name)
        )
        assert version == base_version, (
            "Branched version {!r} != {!r}".format(version, base_version)
        )
        assert dev == "dev", (
            "Branch name doesn't end in -dev: {!r}".format(info["branch"])
        )
        # This is a release branch of this project.
        # Designate this as beta2, dev version based on svn revision.
        return "{}.b2.dev{}{}".format(base_version, info["revision"], modified)

    if info["branch"].startswith("trunk"):
        # This is trunk.
        # Designate this as beta1, dev version based on svn revision.
        return "{}.b1.dev{}{}".format(base_version, info["revision"], modified)

    # This is some unknown branch or tag...
    return "{}.a1.dev{}+{}{}".format(
        base_version,
        info["revision"],
        info["branch"].replace("/", "."),
        modified.replace("+", "."),
    )




#
# Options
#

project_name = "twext"

description = "Extensions to Twisted"

long_description = file(joinpath(dirname(__file__), "README.rst")).read()

url = "http://trac.calendarserver.org/wiki/twext"

classifiers = [
    "Development Status :: 2 - Pre-Alpha",
    "Framework :: Twisted",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: Apache Software License",
    "Operating System :: OS Independent",
    "Programming Language :: Python :: 2.7",
    "Programming Language :: Python :: 2 :: Only",
    "Topic :: Software Development :: Libraries :: Python Modules",
]

author = "Apple Inc."

author_email = "calendarserver-dev@lists.macosforge.org"

license = "Apache License, Version 2.0"

platforms = ["all"]



#
# Dependencies
#

setup_requirements = []

install_requirements = [
    "cffi>=0.6",
    "twisted>=13.2.0",
]

extras_requirements = {
    # Database Abstraction Layer
    "DAL": ["sqlparse>=0.1.11"],

    # LDAP
    "LDAP": ["python-ldap"],

    # OpenDirectory
    "OpenDirectory": ["pyobjc-framework-OpenDirectory"],

    # Oracle
    "Oracle": ["cx_Oracle"],

    # Postgres
    "Postgres": ["PyGreSQL"],
}



#
# Set up Extension modules that need to be built
#

# from distutils.core import Extension

extensions = [
    # Extension("twext.python.sendmsg", sources=["twext/python/sendmsg.c"])
]

if sys.platform == "darwin":
    try:
        from twext.python import launchd
        extensions.append(launchd.ffi.verifier.get_extension())
        from twext.python import sacl
        extensions.append(sacl.ffi.verifier.get_extension())
    except ImportError:
        pass



#
# Run setup
#

def doSetup():
    # Write version file
    version_string = version()
    version_filename = joinpath(dirname(__file__), "twext", "version.py")
    version_file = file(version_filename, "w")
    try:
        version_file.write(
            'version = "{0}"\n\n'.format(version_string)
        )
    finally:
        version_file.close()

    setup(
        name="twextpy",
        version=version_string,
        description=description,
        long_description=long_description,
        url=url,
        classifiers=classifiers,
        author=author,
        author_email=author_email,
        license=license,
        platforms=platforms,
        packages=find_packages(),
        package_data={},
        scripts=[],
        data_files=[],
        ext_modules=extensions,
        py_modules=[],
        setup_requires=setup_requirements,
        install_requires=install_requirements,
        extras_require=extras_requirements,
    )


#
# Main
#

if __name__ == "__main__":
    doSetup()
