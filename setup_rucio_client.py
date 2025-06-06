# Copyright European Organization for Nuclear Research (CERN) since 2012
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import shutil
import sys

from setuptools import setup

if sys.version_info < (3, 9):  # noqa: UP036 (pending https://github.com/rucio/rucio/issues/6971)
    print('ERROR: Rucio Client requires at least Python 3.9 to run.')
    sys.exit(1)

try:
    from setuputil import clients_requirements_table, get_rucio_version
except ImportError:
    sys.path.append(os.path.abspath(os.path.dirname(__file__)))
    from setuputil import clients_requirements_table, get_rucio_version

# Arguments to the setup script to build Basic/Lite distributions
name = 'rucio-clients'
packages = ['rucio', 'rucio.client',
            'rucio.cli', 'rucio.cli.bin_legacy',
            'rucio.common', 'rucio.common.schema',
            'rucio.rse.protocols', 'rucio.rse']
description = "Rucio Client Lite Package"
data_files = [
    ('', ['requirements/requirements.client.txt']),
    ('etc/', ['etc/rse-accounts.cfg.template', 'etc/rucio.cfg.template', 'etc/rucio.cfg.atlas.client.template']),
    ('rucio_client/', ['tools/merge_rucio_configs.py']),
]
scripts = ['bin/rucio', 'bin/rucio-admin']

if os.path.exists('build/'):
    shutil.rmtree('build/')
if os.path.exists('lib/rucio_clients.egg-info/'):
    shutil.rmtree('lib/rucio_clients.egg-info/')
if os.path.exists('lib/rucio.egg-info/'):
    shutil.rmtree('lib/rucio.egg-info/')

# For using SSO login option, install these RPM packages: libxml2-devel xmlsec1-devel xmlsec1-openssl-devel libtool-ltdl-devel

setup(
    name=name,
    version=get_rucio_version(),
    packages=packages,
    package_dir={'': 'lib'},
    data_files=data_files,
    include_package_data=True,
    scripts=scripts,
    author="Rucio",
    author_email="rucio-dev@cern.ch",
    description=description,
    license="Apache License, Version 2.0",
    url="https://rucio.cern.ch/",
    python_requires=">=3.9, <4",
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'License :: OSI Approved :: Apache Software License',
        'Intended Audience :: Information Technology',
        'Intended Audience :: System Administrators',
        'Operating System :: POSIX :: Linux',
        'Natural Language :: English',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
    ],
    install_requires=clients_requirements_table['install_requires'],
    extras_require=clients_requirements_table['extras_require'],
)
